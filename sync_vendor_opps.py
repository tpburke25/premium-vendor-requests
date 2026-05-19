"""
Salesforce → Supabase Sync Script
Non-Alcohol Vendor Requests Portal
Syncs to tbl_all_opportunities (separate from Premium Team CRM)

Runs via GitHub Actions on a schedule.
"""

import os
import sys
import requests
from datetime import datetime, timezone

# ── CONFIG ────────────────────────────────────────────
SF_USERNAME     = os.environ['SF_USERNAME']
SF_PASSWORD     = os.environ['SF_PASSWORD']
SF_INSTANCE_URL = os.environ['SF_INSTANCE_URL']
SUPABASE_URL    = os.environ['SUPABASE_URL']
SUPABASE_KEY    = os.environ['SUPABASE_KEY']

SF_LOGIN_URL    = 'https://login.salesforce.com'
BATCH_SIZE      = 500

# ── SALESFORCE AUTH ───────────────────────────────────
def sf_login():
    print("Authenticating with Salesforce...")
    res = requests.post(f"{SF_LOGIN_URL}/services/oauth2/token", data={
        'grant_type':    'password',
        'client_id':     'PlatformCLI',
        'client_secret': '',
        'username':      SF_USERNAME,
        'password':      SF_PASSWORD,
    })
    if res.ok:
        data = res.json()
        print(f"Logged in via OAuth to {data['instance_url']}")
        return data['access_token'], data['instance_url']

    # SOAP fallback
    import xml.etree.ElementTree as ET
    res = requests.post(f"{SF_LOGIN_URL}/services/Soap/u/57.0",
        headers={'Content-Type': 'text/xml', 'SOAPAction': 'login'},
        data=f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:urn="urn:partner.soap.sforce.com">
  <soapenv:Body>
    <urn:login>
      <urn:username>{SF_USERNAME}</urn:username>
      <urn:password>{SF_PASSWORD}</urn:password>
    </urn:login>
  </soapenv:Body>
</soapenv:Envelope>""")
    if not res.ok:
        print(f"Login failed: {res.text}")
        sys.exit(1)
    import xml.etree.ElementTree as ET
    root = ET.fromstring(res.text)
    ns   = {'sf': 'urn:partner.soap.sforce.com'}
    token    = root.find('.//sf:sessionId', ns).text
    instance = root.find('.//sf:serverUrl', ns).text.split('/services')[0]
    print(f"Logged in via SOAP to {instance}")
    return token, instance


def sf_query(token, instance, soql):
    """Run a SOQL query with automatic pagination. Returns all records."""
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    url     = f"{instance}/services/data/v57.0/query"
    rows    = []
    params  = {'q': soql}
    while True:
        res = requests.get(url, headers=headers, params=params)
        if not res.ok:
            print(f"Query failed ({res.status_code}): {res.text[:500]}")
            return rows
        data = res.json()
        rows.extend(data.get('records', []))
        print(f"  ...fetched {len(rows)} records so far")
        if data.get('done', True):
            break
        url    = instance + data['nextRecordsUrl']
        params = {}
    return rows


# ── SUPABASE UPSERT ───────────────────────────────────
def supabase_upsert(table, rows):
    if not rows:
        print(f"  No rows to upsert for {table}")
        return 0
    headers = {
        'apikey':        SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type':  'application/json',
        'Prefer':        'resolution=merge-duplicates'
    }
    upserted = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        res   = requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=headers,
            json=batch
        )
        if res.ok:
            upserted += len(batch)
            print(f"  Batch {i // BATCH_SIZE + 1}: {len(batch)} rows upserted ✓")
        else:
            print(f"  Batch {i // BATCH_SIZE + 1} error: {res.text[:300]}")
    return upserted


# ── SUPABASE DELETE STALE ─────────────────────────────
def supabase_delete_stale(table, id_field, current_ids):
    """Remove rows from Supabase that are no longer in Salesforce."""
    headers = {
        'apikey':        SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
    }
    existing_ids = []
    page, size = 0, 1000
    while True:
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/{table}?select={id_field}&limit={size}&offset={page * size}",
            headers=headers
        )
        if not res.ok:
            print(f"  Could not fetch existing IDs: {res.text[:200]}")
            return 0
        batch = res.json()
        if not isinstance(batch, list):
            break
        existing_ids.extend(r[id_field] for r in batch if r.get(id_field))
        if len(batch) < size:
            break
        page += 1

    current_set = set(current_ids)
    stale_ids   = [i for i in existing_ids if i not in current_set]

    if not stale_ids:
        print(f"  No stale rows to delete from {table}")
        return 0

    deleted = 0
    for i in range(0, len(stale_ids), BATCH_SIZE):
        batch   = stale_ids[i:i + BATCH_SIZE]
        id_list = ','.join(f'"{id}"' for id in batch)
        res = requests.delete(
            f"{SUPABASE_URL}/rest/v1/{table}?{id_field}=in.({id_list})",
            headers={**headers, 'Content-Type': 'application/json'}
        )
        if res.ok:
            deleted += len(batch)
        else:
            print(f"  Delete batch error: {res.text[:200]}")

    print(f"  Deleted {deleted} stale rows from {table}")
    return deleted


# ── HELPERS ───────────────────────────────────────────
def clean_date(val):
    if not val: return None
    return str(val)[:10]

def clean_int(val):
    if val is None: return None
    try: return int(float(val))
    except: return None

def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ── MAIN SYNC ─────────────────────────────────────────
def sync_vendor_opps(token, instance):
    print("\nSyncing vendor opportunities → tbl_all_opportunities...")

    # One row per Opportunity.
    # Subquery on OpportunityLineItem filters to opps that have at least one
    # APA Premium product line (price > $0, not AccountsFlow).
    # Owner role filter covers all 5 roles that sell this solution.
    soql = """
        SELECT
            Id,
            AccountId,
            Name,
            StageName,
            CloseDate,
            CreatedDate,
            Owner.Name,
            Owner.UserRole.Name,
            Account.Name,
            Account.ParentId,
            Account.Parent.Name,
            Account.FTS_ID__c,
            Account.RecordType.Name,
            Loc__c
        FROM Opportunity
        WHERE IsDeleted = false
          AND StageName = 'Closed Won'
          AND Account.RecordType.Name = 'Retailer'
          AND Owner.UserRole.Name IN (
              'SMB Sales Rep',
              'APA Sales Rep',
              'VP Sales',
              'DAM',
              'APA Sales Manager'
          )
          AND Id IN (
              SELECT OpportunityId
              FROM OpportunityLineItem
              WHERE Product2.Name LIKE '%APA%'
                AND Product2.Name LIKE '%Premium%'
                AND Product2.Name NOT LIKE '%AccountsFlow%'
                AND UnitPrice > 0
          )
          AND OwnerId != null
        ORDER BY CloseDate DESC
    """

    records = sf_query(token, instance, soql)
    print(f"  Pulled {len(records)} records from Salesforce")

    # Deduplicate by opportunity ID (should already be unique, but just in case)
    seen = set()
    rows = []
    for r in records:
        opp_id = r.get('Id')
        if opp_id in seen:
            continue
        seen.add(opp_id)

        acc    = r.get('Account') or {}
        parent = acc.get('Parent') or {}
        owner  = r.get('Owner') or {}

        rows.append({
            'opportunity_id':    opp_id,
            'account_id':        r.get('AccountId'),
            'fts_id':            acc.get('FTS_ID__c'),
            'parent_account_id': acc.get('ParentId'),
            'account_name':      acc.get('Name'),
            'parent_account':    parent.get('Name'),
            'opportunity_name':  r.get('Name'),
            'opportunity_owner': owner.get('Name'),
            'owner_role':        owner.get('UserRole', {}).get('Name') if owner.get('UserRole') else None,
            'stage':             r.get('StageName'),
            'created_date':      clean_date(r.get('CreatedDate')),
            'close_date':        clean_date(r.get('CloseDate')),
            'num_locations':     clean_int(r.get('Loc__c')),
            'synced_at':         now_iso(),
        })

    upserted = supabase_upsert('tbl_all_opportunities', rows)

    # Remove opps that no longer qualify (stage changed, product removed, etc.)
    current_ids = [r['opportunity_id'] for r in rows]
    deleted     = supabase_delete_stale('tbl_all_opportunities', 'opportunity_id', current_ids)

    print(f"  ✓ {upserted} upserted, {deleted} stale removed")
    return len(rows), upserted


# ── MAIN ──────────────────────────────────────────────
def main():
    print("=" * 50)
    print("Non-Alcohol Vendor Requests — Salesforce Sync")
    print(f"Started: {now_iso()}")
    print("=" * 50)

    token, instance = sf_login()
    pulled, upserted = sync_vendor_opps(token, instance)

    print("\n" + "=" * 50)
    print("Sync Summary:")
    print(f"  tbl_all_opportunities: {pulled} pulled, {upserted} upserted")
    print(f"Finished: {now_iso()}")
    print("=" * 50)

if __name__ == '__main__':
    main()
