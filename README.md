<p align="center">
  <img src="https://boreholeai.com/favicon.svg" alt="BoreholeAI" width="80">
</p>

<h1 align="center">BoreholeAI Python SDK</h1>

<p align="center">
  <strong>Geotechnical Borehole Log Digitisation</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/boreholeai/"><img src="https://img.shields.io/pypi/v/boreholeai?label=pypi&color=blue" alt="PyPI"></a>
  <a href="https://pypi.org/project/boreholeai/"><img src="https://img.shields.io/pypi/pyversions/boreholeai" alt="Python"></a>
  <a href="https://github.com/boreholeai/boreholeai-python/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-green" alt="License"></a>
</p>

<p align="center">
  <a href="https://boreholeai.com">Website</a> · <a href="https://boreholeai.com/app/settings/api-keys">Get API Key</a> · <a href="mailto:support@boreholeai.com">Contact</a>
</p>

---

Upload borehole log PDFs or images, get structured ground profiles, test data, and annotated PDFs.

## Background

**The problem:** Geotechnical engineers routinely extract data from borehole log PDFs by hand — reading material descriptions, depths, test results, and keying them into spreadsheets or AGS files. On a typical project with dozens of logs, this takes days. On larger projects, it can take weeks.

**Why it matters:** This matters beyond individual project efficiency. Globally, millions of dollars are spent each year on new ground investigations to understand subsurface conditions — but much of this work has already been done before. Valuable geological and geotechnical records from past projects sit in libraries as old hard copies, in consultancy archives, or on digital platforms that aren't searchable or interoperable. Over time, these reports get misplaced, disposed of, or locked behind organisational boundaries. When that happens, the cycle of discovery starts again from scratch.

**What others have done:** Countries like Denmark, the Netherlands, Switzerland, and the UK have recognised this problem and built centralised, publicly accessible geotechnical databases — some legislated as far back as 1926. In Australia, there is no equivalent national system. Geotechnical data is kept in isolation by individual consultancies, with no regulated or standardised approach to data collection and sharing after project completion. For more on this challenge and what other countries have achieved, see the [Churchill Trust report on geotechnical data capture](https://www.churchilltrust.com.au/project/to-develop-a-statewide-sustainable-gis-geotechnical-database-to-capture-present-data-for-the-future/).

**Our approach:** We built BoreholeAI because we believe that better geotechnical data infrastructure starts with making it easier to get data out of the documents where it's currently trapped. BoreholeAI reads borehole log PDFs (scanned or digital) and extracts the geotechnical data — with depth and spatial awareness — into structured formats like Excel and AGS.

## What It Does

**Extracts:**
- Ground profile — material descriptions, depths, geology, consistency/density
- Test data — SPT, UCS, Is50, groundwater levels
- Borehole metadata — hole ID, location, dates, drilling method

**Outputs:**
- `Borehole_ground_profile.xlsx` — structured ground profile table
- `Borehole_test_data.xlsx` — all test results in tabular format
- `Borehole_ags4.ags` — industry-standard AGS4 data transfer file
- `*_annotated.pdf` — original document with extracted regions highlighted

## Examples

See the [quickstart notebook](examples/quickstart.ipynb) for an interactive walkthrough, or follow the steps below.

## Installation

```bash
pip install boreholeai
```

## Quick Start

```python
from boreholeai import BoreholeAI

client = BoreholeAI(api_key="bhai_your_api_key_here")

# Process a single borehole log
result = client.process_documents("BH01.pdf", output_dir="./results")

print(f"Pages processed: {result.num_pages}")
for f in result.files:
    print(f"  {f.filename}")
```

## Folder / Batch Processing

Pass a directory path to process multiple files together. Results are merged into a single ground profile Excel, test data Excel, and AGS file, with one annotated PDF per input file.

```python
result = client.process_documents("./borehole_logs/", output_dir="./results")

# Output:
#   Borehole_ground_profile.xlsx   (merged from all input files)
#   Borehole_test_data.xlsx        (merged from all input files)
#   Borehole_ags4.ags              (merged from all input files)
#   BH01_annotated.pdf             (one per input file)
#   BH02_annotated.pdf
#   BH03_annotated.pdf
```

## Supported File Types

- PDF (`.pdf`)
- PNG (`.png`)
- JPEG (`.jpg`, `.jpeg`)
- TIFF (`.tif`, `.tiff`)
- WebP (`.webp`)

## API Key

Get your API key from the [BoreholeAI dashboard](https://boreholeai.com/app/settings/api-keys).

```python
# Pass directly
client = BoreholeAI(api_key="bhai_xxx")

# Or for local development / testing
client = BoreholeAI(api_key="bhai_xxx", base_url="http://localhost:8000")
```

## Error Handling

```python
from boreholeai import BoreholeAI, InsufficientCreditsError, AuthenticationError

client = BoreholeAI(api_key="bhai_xxx")

try:
    result = client.process_documents("borehole.pdf")
except AuthenticationError:
    print("Invalid API key")
except InsufficientCreditsError:
    print("Not enough credits — buy more at boreholeai.com")
```

## Response

`process_documents()` returns a `JobResult`:

```python
@dataclass
class JobResult:
    job_id: str              # Unique job identifier
    status: str              # "completed"
    num_pages: int           # Total pages processed
    credits_used: int        # Credits consumed
    files: list[FileResult]  # Downloaded result files

@dataclass
class FileResult:
    filename: str            # e.g. "Borehole_ground_profile.xlsx"
    path: Path               # Local path where file was saved
```

## Accuracy

BoreholeAI uses a sophisticated multi-agent, multi-stage agentic system that combines engineering deterministic algorithms, customized deep learning models, and computer vision with AI-assisted document understanding. Your documents are never read directly by AI models — we apply an OCR intermediary layer so that AI only works with extracted text and layout information, never with your original files. The structured output is driven by spatial reasoning and rule-based logic, not generative models.

The system handles complex layouts, varying scales, multi-page logs, and inconsistent formatting.

We have tested extensively across a wide range of borehole log formats and consistently achieve 95-100% accuracy. We are grateful to the [Queensland Geotechnical Database (QGD)](https://www.qgd.com) for making their data openly available — their collection of real-world borehole logs has been invaluable for testing and validating the system.

That said, borehole logs vary significantly in layout and formatting. If you find an inaccuracy or an extraction issue, please [let us know](mailto:support@boreholeai.com) — every report helps us improve the system.

**Expected input:** Properly formatted borehole log PDFs or clean scanned copies. Photos taken on site (e.g. phone camera shots of printed logs) are not supported and may produce unreliable results.

## Current Scope and Limitations

- **Standard alignment:** Built and validated against **Australian Standard AS 1726** borehole log formats. Logs following other national standards may work but are not yet validated.
- **Document types:** Borehole logs (BH), pavement core logs (PCP), and test pit logs (TP). Other geotechnical documents (e.g. CPT plots, lab reports) are not currently supported.
- **Languages:** English only. Other languages may partially work but are not officially supported.
- **Units:** Metric only — depths and dimensions must be in metres. Logs in feet or other imperial units are not currently supported.
- **Scan quality:** Logs should be scanned orthogonally. Tilted scans work within approximately 10°, but accuracy drops beyond that.
- **Watermarks:** PDFs with heavy watermarks, especially tilted ones, will reduce accuracy — particularly for strength and test data extraction. Upload a clean copy where possible.
- **Handwriting:** Clear handwriting is supported. Extremely unclear handwriting may affect results.
- **File size:** Maximum 20 MB per file.

## Credits

BoreholeAI uses a credit system (1 credit = 1 page) to cover the computational cost of running the AI models. Every new account (individuals) receives free credits to get started, and additional free credits are available on request — just [get in touch](mailto:support@boreholeai.com).

For those who'd like to support the ongoing development of the project and help keep it running, credits can also be [purchased here](https://boreholeai.com/app/settings/plan). This is entirely optional — the goal is to make geotechnical data more accessible, and we'd rather people use the tool than not.

[Contact us](mailto:support@boreholeai.com)

## Enterprise

We understand that many organisations have strict data governance requirements and cannot send documents to external services. BoreholeAI can be deployed within your own infrastructure — on-premise or in your private cloud — so that your data never leaves your environment.

If this is a requirement for your team, [contact us](mailto:support@boreholeai.com) to discuss deployment options.

## Disclaimer

While every effort has been made to ensure accuracy and reliability, BoreholeAI is a tool to assist — not replace — professional judgement. It is the user's responsibility to verify that all extracted data is accurate and fit for their intended purpose, including compliance with applicable design specifications, standards, and regulatory requirements.

## License

Apache-2.0
