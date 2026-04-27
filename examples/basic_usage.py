"""Basic usage example for the BoreholeAI Python SDK (0.4.0+)."""

from boreholeai import BoreholeAI, AuthenticationError, InsufficientCreditsError

# Initialize client with your API key.
# Get one at https://boreholeai.com/app/settings/api-keys
client = BoreholeAI(api_key="bhai_your_api_key_here")


# ----------------------------------------------------------------------
# Single file
# ----------------------------------------------------------------------
result = client.process_documents("path/to/borehole.pdf", output_dir="./results")

print(f"Status: {result.status}")            # "completed" | "partial" | "failed"
print(f"Job ID: {result.job_id}")
print(f"Pages processed: {result.num_pages}")
print(f"Credits used: {result.credits_used}")
print("Output files:")
for f in result.files:
    print(f"  {f.filename} → {f.path}")


# ----------------------------------------------------------------------
# Folder of PDFs / images — fans out into N concurrent server-side jobs
# and merges results client-side. Default concurrency = 6; override per
# your worker pool size.
# ----------------------------------------------------------------------
result = client.process_documents(
    "path/to/logs/",
    output_dir="./results",
    concurrency=6,
)

print(f"\nStatus: {result.status}")
print(f"Successes: {result.successes}")      # list of input filenames
print(f"Failures: {result.failures}")        # filename → error (empty if all good)
print(f"All job IDs: {result.job_ids}")
print("Merged outputs:")
for f in result.files:
    print(f"  {f.filename} → {f.path}")


# ----------------------------------------------------------------------
# Resume after interrupt: just re-run with the same output_dir.
# A manifest (`output_dir/.boreholeai_manifest.json`) tracks progress;
# already-completed files are skipped automatically.
# ----------------------------------------------------------------------
# (no special call — Ctrl-C, then re-invoke the same command)


# ----------------------------------------------------------------------
# Partial failure: the call succeeds even if some files fail server-side.
# Inspect `result.failures` to find which files need attention.
# ----------------------------------------------------------------------
result = client.process_documents("path/to/logs/", output_dir="./results")
if result.status == "partial":
    print(f"\n{len(result.failures)} file(s) failed:")
    for filename, error in result.failures.items():
        print(f"  {filename}: {error}")


# ----------------------------------------------------------------------
# Hard errors (auth, credits) raise immediately.
# ----------------------------------------------------------------------
try:
    result = client.process_documents("path/to/logs/")
except AuthenticationError:
    print("Invalid API key")
except InsufficientCreditsError:
    print("Not enough credits — request more at support@boreholeai.com")
