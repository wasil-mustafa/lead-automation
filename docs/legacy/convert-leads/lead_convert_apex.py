"""
Lead Conversion — Bulk via Salesforce CLI + Apex
-----------------------------------------------------------------
Replaces the prior REST /actions/standard/leadConvert approach,
which does not exist as a standard Salesforce action.

Pipeline:
  1. Query unconverted Lead IDs from a campaign.
  2. Build an Apex script for each batch that calls
     Database.convertLead() in bulk.
  3. Run the Apex via `sf apex run --json`.
  4. Parse the serialized result from the debug log.
  5. Write per-lead success / failure to a CSV log.

Note on duplicates: Apex does NOT expose a public bypass for
duplicate rules during convertLead — the setBypass* methods
that exist on Database.LeadConvert are marked internal-only and
reject calls from customer code. If a duplicate rule blocks
conversion, change its action from "Block" to "Allow" (with
alert) in Setup → Duplicate Rules, or deactivate it for the run.
"""

import csv
import json
import os
import subprocess
import time
from datetime import datetime

# ── CONFIG ──────────────────────────────────────────────────────────

ORG_ALIAS = "myOrg"

CAMPAIGN_ID = "701Mz00001Iru7fIAB"
OWNER_ID = "005Mz00000XvtQzIAJ"

CONVERTED_STATUS = "Qualified"

# Database.convertLead supports up to 100 LeadConvert objects
# per call. 50 is a comfortable default that leaves headroom
# for triggers/flows running on Account / Contact insert.
BATCH_SIZE = 10

WAIT_SECONDS = 1  # Pause between batches.

DO_NOT_CREATE_OPPORTUNITY = True

OUTPUT_DIR = r"C:\Users\WasilMustafa\OneDrive - kununu\Apex Script\Convert Leads"

TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")

LOG_FILE = os.path.join(
    OUTPUT_DIR,
    f"convert_log_{TIMESTAMP}.csv"
)

APEX_TEMP_FILE = os.path.join(
    OUTPUT_DIR,
    f"_convert_batch_{TIMESTAMP}.apex"
)

# Markers used to fish the serialized result out of the debug log.
RESULT_START = "CONVERT_RESULTS_START"
RESULT_END = "CONVERT_RESULTS_END"

# ────────────────────────────────────────────────────────────────────


def apex_escape(value):
    """Escape a string for safe interpolation into Apex single-quoted literals."""
    if value is None:
        return ""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def run_cli_json(cmd):
    """Run a sf CLI command and parse its JSON output."""

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        shell=True
    )

    if not result.stdout:
        raise RuntimeError(
            f"CLI returned no stdout.\n"
            f"Return code: {result.returncode}\n"
            f"STDERR: {result.stderr}"
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Could not parse CLI JSON output: {e}\n"
            f"STDOUT (first 2000 chars):\n{result.stdout[:2000]}"
        )


def query_lead_ids():
    """Pull unconverted Lead IDs from the campaign."""

    soql = (
        "SELECT LeadId "
        "FROM CampaignMember "
        f"WHERE CampaignId = '{CAMPAIGN_ID}' "
        "AND Lead.IsConverted = false "
        "ORDER BY LeadId"
    )

    cmd = (
        f'sf data query --query "{soql}" '
        f'--target-org {ORG_ALIAS} --json'
    )

    print("Querying leads from campaign...")
    data = run_cli_json(cmd)

    records = data.get("result", {}).get("records", [])

    lead_ids = [
        rec["LeadId"]
        for rec in records
        if rec.get("LeadId")
    ]

    print(f"Found {len(lead_ids)} unconverted lead(s).")
    return lead_ids


def build_apex_script(lead_ids):
    """Construct the Apex script for a single batch of leads."""

    no_opp = "true" if DO_NOT_CREATE_OPPORTUNITY else "false"

    status_esc = apex_escape(CONVERTED_STATUS)
    owner_esc = apex_escape(OWNER_ID)

    lines = [
        "List<Database.LeadConvert> reqs = "
        "new List<Database.LeadConvert>();"
    ]

    for lid in lead_ids:
        lid_esc = apex_escape(lid)
        lines.append(
            "{ "
            "Database.LeadConvert lc = new Database.LeadConvert(); "
            f"lc.setLeadId('{lid_esc}'); "
            f"lc.setConvertedStatus('{status_esc}'); "
            f"lc.setDoNotCreateOpportunity({no_opp}); "
            f"lc.setOwnerId('{owner_esc}'); "
            "reqs.add(lc); "
            "}"
        )

    # allOrNone = false → partial successes allowed; failures don't
    # roll back the rest of the batch.
    lines.append(
        "Database.LeadConvertResult[] results = "
        "Database.convertLead(reqs, false);"
    )

    lines.append("""
List<Map<String, Object>> output = new List<Map<String, Object>>();
for (Integer i = 0; i < results.size(); i++) {
    Database.LeadConvertResult r = results[i];
    Map<String, Object> row = new Map<String, Object>();
    row.put('leadId', r.getLeadId());
    row.put('success', r.isSuccess());
    row.put('accountId', r.getAccountId());
    row.put('contactId', r.getContactId());
    row.put('opportunityId', r.getOpportunityId());
    if (!r.isSuccess()) {
        List<String> errs = new List<String>();
        for (Database.Error e : r.getErrors()) {
            errs.add(
                String.valueOf(e.getStatusCode())
                + ': ' + e.getMessage()
            );
        }
        row.put('errors', String.join(errs, ' | '));
    } else {
        row.put('errors', '');
    }
    output.add(row);
}
""".strip())

    # LoggingLevel.ERROR is always captured, regardless of the
    # org's default debug level — keeps us safe from trace flag drift.
    lines.append(
        "System.debug(LoggingLevel.ERROR, "
        f"'{RESULT_START}' + JSON.serialize(output) + '{RESULT_END}');"
    )

    return "\n".join(lines)


def run_apex_batch(lead_ids):
    """Write Apex to temp file, execute via CLI, return parsed results."""

    apex_code = build_apex_script(lead_ids)

    with open(APEX_TEMP_FILE, "w", encoding="utf-8") as f:
        f.write(apex_code)

    cmd = (
        f'sf apex run --file "{APEX_TEMP_FILE}" '
        f'--target-org {ORG_ALIAS} --json'
    )

    data = run_cli_json(cmd)

    # On success, the Apex outcome lives under `result`.
    # On failure (compile or runtime), it lives under `data`,
    # with a top-level `message` field summarizing the issue.
    result_block = data.get("result") or data.get("data") or {}
    top_message = data.get("message", "")

    # Compile check
    if not result_block.get("compiled", False):
        problem = (
            result_block.get("compileProblem")
            or top_message
            or "No compile error message returned by CLI."
        )
        raise RuntimeError(f"Apex compile failed:\n{problem}")

    # Runtime check
    if not result_block.get("success", False):
        exc_msg = (
            result_block.get("exceptionMessage")
            or top_message
            or "No runtime error message returned by CLI."
        )
        exc_trace = result_block.get("exceptionStackTrace", "")
        raise RuntimeError(
            f"Apex runtime failed:\n{exc_msg}\n{exc_trace}"
        )

    logs = result_block.get("logs", "")

    start_idx = logs.find(RESULT_START)
    if start_idx == -1:
        raise RuntimeError(
            "Result start marker not found in Apex log.\n"
            f"Log preview:\n{logs[:1500]}"
        )

    end_idx = logs.find(RESULT_END, start_idx + len(RESULT_START))
    if end_idx == -1:
        raise RuntimeError(
            "Result end marker not found in Apex log.\n"
            f"Log preview:\n{logs[:1500]}"
        )

    json_str = logs[start_idx + len(RESULT_START):end_idx]

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Could not parse result JSON: {e}\n"
            f"JSON preview:\n{json_str[:1500]}"
        )


def main():

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    lead_ids = query_lead_ids()

    if not lead_ids:
        print("Nothing to do.")
        return

    total_batches = (
        len(lead_ids) + BATCH_SIZE - 1
    ) // BATCH_SIZE

    print(f"\nBatch size: {BATCH_SIZE}")
    print(f"Total batches: {total_batches}")
    print(f"Log file: {LOG_FILE}\n")

    # Initialize log file with header
    with open(LOG_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Batch", "LeadId", "Status",
            "AccountId", "ContactId", "OpportunityId", "Error"
        ])

    total_success = 0
    total_fail = 0

    for i in range(0, len(lead_ids), BATCH_SIZE):

        batch = lead_ids[i:i + BATCH_SIZE]
        batch_index = i // BATCH_SIZE + 1

        print(
            f"Executing batch {batch_index}/{total_batches} "
            f"({len(batch)} leads)..."
        )

        try:
            results = run_apex_batch(batch)
        except Exception as e:
            # Whole batch blew up (compile error, runtime exception,
            # missing log markers, etc). Log every lead as failed.
            print(f"  BATCH FAILED: {e}")

            with open(LOG_FILE, "a", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                for lid in batch:
                    writer.writerow([
                        batch_index, lid, "BATCH_ERROR",
                        "", "", "", str(e)
                    ])

            total_fail += len(batch)

            if batch_index < total_batches and WAIT_SECONDS > 0:
                time.sleep(WAIT_SECONDS)
            continue

        batch_success = 0
        batch_fail = 0

        with open(LOG_FILE, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)

            for row in results:
                lid = row.get("leadId", "") or ""

                if row.get("success"):
                    writer.writerow([
                        batch_index, lid, "SUCCESS",
                        row.get("accountId", "") or "",
                        row.get("contactId", "") or "",
                        row.get("opportunityId", "") or "",
                        ""
                    ])
                    batch_success += 1
                else:
                    error_text = row.get("errors", "") or ""
                    writer.writerow([
                        batch_index, lid, "FAIL",
                        "", "", "", error_text
                    ])
                    print(f"  FAIL: {lid} | {error_text}")
                    batch_fail += 1

        total_success += batch_success
        total_fail += batch_fail

        print(
            f"  → Batch {batch_index} done "
            f"| Success: {batch_success} "
            f"| Failed: {batch_fail}"
        )

        if batch_index < total_batches and WAIT_SECONDS > 0:
            time.sleep(WAIT_SECONDS)

    # Cleanup temp Apex file
    try:
        if os.path.exists(APEX_TEMP_FILE):
            os.remove(APEX_TEMP_FILE)
    except OSError:
        pass

    print(f"\n{'=' * 60}")
    print(
        f"DONE | Total Success: {total_success} "
        f"| Total Failed: {total_fail}"
    )
    print(f"Log file: {LOG_FILE}")


if __name__ == "__main__":
    main()
