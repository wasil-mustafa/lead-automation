# Review gate

Stage 1 uploads a `qa_artifact.xlsx` whose **Review** sheet has an empty `Approve?` column.

To approve Leads for conversion:
1. Open the QA artifact (download from the Stage 1 workflow run's artifacts).
2. Set `Approve?` to `yes` on the rows you want converted. Blank out `account_match_id`
   or `contact_match_id` on a row to force create-new instead of linking.
3. Export the Review sheet (the approved rows) to `review/approved_<date>.csv` and commit it.
4. Run the **Stage 2** workflow — it converts only the approved rows (newest
   `approved_*.csv` by default) once a required reviewer signs off on the `lead-conversion`
   environment.

Stage 2's conversion Apex re-filters `IsConverted = false`, so re-running never
double-converts.
