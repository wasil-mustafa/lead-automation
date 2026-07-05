# Legacy: manual bulk lead conversion (May 2026)

Reference copies of the two files worth keeping from the untracked `Convert Leads/` folder —
the manual campaign-based bulk-conversion tooling that predates `src/leadauto/` and was fully
superseded by `src/leadauto/convert/converter.py` (Stage 2). The rest of that folder
(four script generations, one-off apex helpers, ~35 MB of run logs) is gitignored and can be
deleted at any time.

## Files

- **`lead_convert_apex.py`** — the final generation of the manual converter and the direct
  ancestor of `convert/converter.py`: bulk `Database.convertLead(reqs, false)` via anonymous
  Apex, results `JSON.serialize`d between sentinel markers and parsed out of the debug log.
  Kept as provenance for the sentinel/bulk design.
- **`convertLead.apex`** — single-lead convert that first patched the Lead to pass the org's
  validation rules. Documents which fields blocked conversion in practice (May 2026):
  `Street = ','`, `KN_Employee__c = '11-50'`, `KN_NoOwnDBID__c = true`.

## Lessons encoded here (relevant to Stage 2 go-live)

1. **Duplicate rules can block `Database.convertLead`, and Apex cannot bypass them** — the
   `setBypass*` methods on `Database.LeadConvert` are internal-only.
   `Database.DMLOptions.DuplicateRuleHeader.AllowSave` works for plain DML inserts but does
   not apply to lead conversion. The REST route (`Sforce-Duplicate-Rule-Header: allowSave=true`
   on `/actions/standard/leadConvert`) was tried (`convertLeads.py` in the legacy folder) and
   abandoned. Workarounds: fix the underlying duplicate, or change the rule from Block to
   Alert in Setup.
2. **Validation rules block conversion on sparse leads** — see `convertLead.apex` above for
   the exact field patches that were needed.
