# GameTermLab | 遊戲術語擷取工具

Source-backed game terminology extraction.

GitHub account: digimarketingai
Repository: gametermlab

## Purpose

Select a supported game or upload authorized source files.
Extract structured names and reviewable language candidates.
Export the results with provenance.

This is not a generative game-dialogue tool.

## Built-in sources

### League of Legends

Source: Riot Data Dragon.

Collects:
- Champion names.
- Champion titles.
- Passive and active ability names.
- Item names.
- Available descriptions for NLP analysis.

The dataset version is resolved at runtime.

The selected Data Dragon version is not proof of your regional
client version.

### Pokémon franchise

Source: PokéAPI community dataset.

Collects:
- Species names.
- Move names.
- Location names.
- Item names.
- One available English flavor-text entry per resource,
  where available.

This connector is franchise-wide, not filtered to an individual
Pokémon game release.

Species are categorized as Creature.
It does not provide a structured trainer/NPC list.

### Other games

Upload your own authorized source files.

Supported:
- UTF-8 TXT.
- UTF-8 CSV.
- UTF-8 JSON in the documented record format.

This version does not parse proprietary game archives,
executables, encrypted assets, audio, or screenshots.

## Installation

Use a dedicated Python environment.

```bash
python -m venv .venv
```

Activate on macOS/Linux:

```bash
source .venv/bin/activate
```

Activate on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
python -m pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

Run:

```bash
python app.py
```

Open the local address printed in the terminal.

## Workflow

1. Choose a game.
2. Choose Live connector or Uploaded files.
3. Select extraction categories.
4. Set a modest batch size.
5. Confirm you are authorized to use the sources.
6. Click Start extraction.
7. Read warnings.
8. Review candidates.
9. Download the exports.

Choosing "Other game" always uses uploaded files.

## Upload schema

### CSV

Required column:
- text

Optional columns:
- id
- category
- speaker
- version

Recognized category values:
- character
- creature
- move
- place
- item
- term
- text
- dialogue

For structured categories, the entire text cell must be the term.

Use text for ordinary prose.
Use dialogue only for actual dialogue.

### JSON

Use a list of records:

```json
[
  {
    "id": "your-source-record-id",
    "category": "dialogue",
    "speaker": "speaker-as-recorded-in-your-source",
    "version": "your-source-version",
    "text": "Replace this with a line from your authorized source."
  }
]
```

The example above describes the format.
It is not an authentic game quotation.

### TXT

Each non-empty line becomes one source field.

Enable "Treat TXT lines as dialogue" only for actual dialogue files.

## Results

### Structured entries

Copied from identified dataset fields or user-labeled upload records.

User-labeled categories are not independently verified.

### NLP candidates

- Character: PERSON entity candidates.
- Place: location-related entity candidates.
- Term: noun-chunk terminology candidates.
- Phrase: multiword noun chunks.
- Clause: selected dependency-subtree spans.
- Expression: short sentences from dialogue records.

These classifications require review.

The English model may miss fictional names or misclassify them.
A noun phrase is not automatically a specialized game term.
A short dialogue sentence is not automatically an idiom.

## Exports

The ZIP contains:

- terms.csv
- occurrences.csv
- gametermlab.xlsx
- results.json
- sources.json
- manifest.json
- Per-category CSV files where results exist

The Terms sheet summarizes category/term combinations.
The Occurrences sheet preserves extracted spans and provenance.

## Provenance

Occurrence records include:

- Game label.
- Category.
- Extracted text.
- Extraction method.
- Review status.
- Source identifier.
- Source URL or uploaded-file label.
- Source field.
- Version.
- Speaker where supplied.
- Start/end offsets.
- Nearby context.

Offsets refer to normalized source-field text, not raw file bytes.

API HTML descriptions are converted to visible text.
Whitespace is normalized.

Source metadata includes a hash of the normalized field.
Uploaded-file labels also include a hash of the original file.

Full source texts are not bundled.

## Counts

"Extracted mentions" means occurrences emitted by this extractor.

It does not mean:
- Whole-game frequency.
- All literal string occurrences.
- Number of players using the term.
- A measure of terminology importance.

Repeated source records can increase counts.

## Limits

- English NLP only.
- Up to 30 live records per source group per run.
- Up to 10 uploaded files.
- 1 MB per uploaded file.
- 6,000 characters per normalized field.
- 150,000 characters per normalized corpus.
- 3,000 source fields.
- 30,000 extracted occurrences.

A live run is a bounded batch, not a complete game corpus.

## Partial results

If source collection fails after some fields were loaded,
the app may export those fields with a PARTIAL COLLECTION warning.

Always inspect the manifest and warnings.

No replacement source is silently substituted.

## API behavior

Public API responses are cached locally for one hour.

Uncached requests are paced.
Automatic retries are not implemented.
Redirects are not followed.
Only the built-in public API hosts can be requested.

## Privacy

Uploaded text is analyzed on the hosting machine.

It is not sent to a language-model service by this code.

Running locally is recommended for private research.

Export files remain in the host's temporary directory until removed.
Public API cache files are stored under:

```text
~/.gametermlab/
```

Do not upload confidential material to an untrusted host.

## Public sharing

The default launch is local.

To enable an authenticated share link, set:

- GAMETERMLAB_USER
- GAMETERMLAB_PASSWORD

Then run:

```bash
python app.py --share
```

Use strong credentials.

This authentication option does not turn the prototype into
a production-grade multi-tenant service.

## Responsible source use

Use authorized sources and respect their usage conditions.

The app does not:
- Bypass access controls.
- Decrypt game archives.
- Circumvent DRM.
- Verify redistribution rights.
- Verify the authenticity of uploaded transcripts.
- Grant permission to republish extracted content.

## Testing status

This implementation has not been execution-tested as part
of its preparation.

Test installation, source availability, extraction quality,
and exports before classroom or production use.
