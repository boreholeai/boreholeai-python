# Macro template for the ground profile workbook

When `macro_button=True` is passed to `process_documents` / `merge_results`,
the SDK builds the ground profile workbook on top of
`ground_profile_template.xlsm` (this folder) and saves it as `.xlsm`. The
template carries a VBA macro and a button on the Processing Info sheet that
lets reviewers rebuild the Geology / Consistency_Density / USCS tabs from a
hand-corrected Material tab.

`ground_profile_template.xlsm` cannot be generated programmatically — VBA
projects can only be authored by Excel. It is produced once, by hand, from
the two files that ARE in this folder:

- `ground_profile_template_base.xlsx` — workbook with the "Processing
  Info" sheet AND the form-control button already drawn and assigned
  (`[0]!RegenerateDerivedTabs`). Everything except the VBA itself, which
  an `.xlsx` cannot store.
- `RegenerateDerivedTabs.bas` — the VBA source (a port of
  `consolidate_intervals` + `drop_zero_thickness_rows` from the backend's
  `data_processing_utils.py`; keep them in sync).

## Re-authoring (whenever the .bas changes)

1. Open `ground_profile_template_base.xlsx` in Excel.
2. Tools → Macro → Visual Basic Editor → **File → Import File…** → pick
   `RegenerateDerivedTabs.bas`. Close the editor. (The button is already
   drawn and assigned — do not touch it.)
3. File → Save As → format **Excel Macro-Enabled Workbook (.xlsm)** →
   save as `ground_profile_template.xlsm` in THIS folder, replacing the
   old one. Do not overwrite the `.xlsx` base.

Authoring pitfalls learned the hard way (first authored 2026-07-27):
- The module name in the `.bas` must differ from the `Sub` name, or
  Excel rejects button assignment with "Formula is too complex to be
  assigned to object" (hence `modRegenerateTabs`).
- In the Assign Macro dialog the "Macro name" field must be filled by
  CLICKING the macro in the list — typed text (e.g. a label with `/`)
  is parsed as a formula and fails the same way.
- A plain ⌘S saves to the `.xlsx` and silently strips the VBA — the
  Save As to `.xlsm` is the step that actually captures the macro.

## Verifying

Run a merge with `macro_button=True`, open the output `.xlsm`, click
"Enable Macros", correct something in the Material tab, then click the
button on the Processing Info sheet. The three derived tabs should rewrite
themselves (a summary MsgBox reports the row counts).

## If the button does not survive the merge

openpyxl preserves the VBA project and the button's VML part
(`keep_vba=True`), which is what Excel renders form-control buttons from.
If a future openpyxl version drops the button (macro still present but no
button visible), reviewers can still run it via Developer → Macros →
`RegenerateDerivedTabs`, and the merge post-processing should be revisited.
