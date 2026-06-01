# SimplyJudge Organiser Handbook

This guide is for event organisers who use SimplyJudge to upload photos, manage judging, and share feedback or results.

## 1. Your Event Workspace

After logging in, you will see **Your events**. Open the event you want to manage.

Depending on your event setup, your workspace may be one of two types:

- **Full competition funnel**: used for competitions with triage, shortlist rounds, and final judging.
- **Feedback portal**: used when every photo should receive scores and written feedback.

If you cannot see the correct event, contact the platform admin. Your account must be assigned to that event.

## 2. User Roles

Each user is assigned per event.

- **Competition Organizer**: uploads data, uploads photos, checks import jobs, and views reports.
- **Internal Reviewer**: helps with internal triage and Round 1 review in full competitions.
- **VIP Guest Judge**: scores photos in the final judging round or reviews photos in a feedback portal.

One person can have more than one role. For example, an organiser can also be a reviewer.

## 3. Choosing the Correct Workflow

### Full Competition Funnel

Use this for award-style competitions such as Youth POTY.

Typical flow:

1. Upload rubric and entry/photo data.
2. Internal reviewers complete triage.
3. Internal reviewers score Round 1.
4. Organizer finalizes the shortlist.
5. VIP guest judges score shortlisted photos.
6. Leaderboard and reports can be reviewed.

### Feedback Portal

Use this for events such as Shutter Society, where every photo needs feedback.

Typical flow:

1. Upload rubric and entry/photo data.
2. Assigned judges/reviewers score every photo.
3. Public feedback report can be shared with members.

Feedback portals do not use triage, Round 1, or shortlist finalization.

## 4. Uploading Data

Open your event and select **Upload data**.

You can upload:

- A rubric CSV.
- A CSV with entry rows only.
- A ZIP package containing `EntryForm.csv` and all photo files.
- A photo-only ZIP when every photo filename is already the unique reference code.
- A direct ZIP download link from Dropbox, Google Drive, WeTransfer, or similar services, if the link downloads the ZIP directly.

For most events, use the ZIP package sync because it imports the spreadsheet and matches the image files in one process.

## 5. Rubric Spreadsheet Format

Upload as a `.csv` file.

Required columns:

```csv
Criterion Name,Description,Weight
Composition,Strength of framing and visual structure,1.0
Originality,Freshness of idea and execution,1.0
Technical Quality,"Focus, exposure, and post-processing quality",1.0
```

Accepted column names:

- `Criterion Name` or `Criterion`
- `Description`
- `Weight`

Notes:

- Weight can be `1.0` for normal weighting.
- Higher weights make a criterion count more heavily.
- Keep criterion names short and judge-friendly.

## 6. Entry Spreadsheet Format

Upload as a `.csv` file, or place it inside a ZIP package as `EntryForm.csv`.

Recommended columns:

```csv
Title,Photographer,Category,Code,Image,Description,Camera Settings
Quiet Horizon,Jane Smith,Landscape,1001,quiet-horizon.jpg,A short story about the image.,"Nikon Z8, 50mm, f/4, ISO 200"
```

Accepted column names:

- Title: `Title`
- Photographer: `Photographer`, `Photographer Name`, or `photographer_name`
- Category: `Category`
- Entry code: `Code`, `ID`, `Number`, `Entry ID`, `Entry Code`, or `id`
- Image reference: `Image`, `Image File`, `Filename`, `File Name`, `Photo File`, `Photo Filename`, or `Asset`
- Description/story: `Description` or `Story`
- Camera settings: `Camera Settings`, `Settings`

Important:

- Save the spreadsheet as CSV, not XLSX.
- Keep each entry on its own row.
- If you use `Code`, make sure every code is unique.
- Do not reuse the same entry code in another event.

## 7. ZIP Package Format

Your ZIP should include:

- `EntryForm.csv`
- All photo files

The CSV can be anywhere in the ZIP, but naming it `EntryForm.csv` is best.

Images can be inside folders. SimplyJudge matches photos by:

- Entry code.
- Image filename/reference column.
- Photo title.
- Filename stem.

Example ZIP:

```text
ypoty_entries.zip
  EntryForm.csv
  images/
    quiet-horizon.jpg
    city-light-1002.jpg
    portrait-final.jpeg
```

Recommended photo file types:

- `.jpg`
- `.jpeg`
- `.png`
- `.webp`
- `.tif`
- `.tiff`

## 8. Photo-Only Uploads

Use this when the photos have already been renamed to their unique IDs.

On the upload screen, choose **Filenames are reference codes** before starting the chunked ZIP upload or remote ZIP sync. This tells SimplyJudge not to look for `EntryForm.csv`.

Example ZIP:

```text
shutter-society-photos.zip
  SS001.jpg
  SS002.jpg
  SS010.jpg
```

No `EntryForm.csv` is required.

SimplyJudge will:

- Sort the photos by filename.
- Create one entry per photo.
- Use the filename without the extension as the entry reference code.
- Show that reference code in the feedback report.

For example, `SS001.jpg` becomes an entry with reference code `SS001`.

The filename code must be unique inside the ZIP.

## 9. Large ZIP Uploads

Use **Chunked ZIP package sync** for local uploads. This sends the ZIP in smaller pieces and avoids browser timeout problems.

Use **Remote ZIP sync** for very large packages. Paste a direct download URL.

For Dropbox, the link should normally end with:

```text
dl=1
```

Avoid links that open a preview page instead of downloading the ZIP directly.

## 10. After Sync Finishes

The import status page shows:

- Entries discovered
- Entries committed
- Images matched
- Job status

If the job succeeds, choose the next step:

- **Review Imported Entries**: check what was imported.
- **Upload More Data**: add another file or corrected package.
- **Start Triage Review**: only for full competitions where you are also an internal reviewer.
- **Review Photos**: for feedback portal reviewers.

If the job fails, no partial import should remain. Fix the spreadsheet or ZIP and upload again.

## 11. Public Outputs

Leaderboards are public to anyone with the link.

Feedback portal reports are public to anyone with the link.

Full competition feedback ledgers are not public by default. They are visible to organisers.

## 12. Common Mistakes

- Uploading XLSX instead of CSV.
- Forgetting to include `EntryForm.csv` in the ZIP.
- Using a spreadsheet ZIP when you meant to use photo-only import.
- Reusing the same filename code twice in a photo-only ZIP.
- Using a Dropbox/Drive link that opens a preview page instead of downloading the ZIP.
- Reusing entry codes from another event.
- Naming image files in a way that does not match the CSV.
- Uploading photos without a rubric configured.

## 13. Quick Checklist Before Uploading

- Event workflow is correct.
- Organisers and judges are assigned.
- Rubric CSV has `Criterion Name`, `Description`, and `Weight`.
- Entry CSV is named `EntryForm.csv` if uploading a ZIP.
- Each entry has a unique code or clear image filename.
- Photo files are inside the ZIP.
- For photo-only imports, every filename stem is the reference code.
- Remote links are direct ZIP download links.
- After upload, check entries committed and images matched.
