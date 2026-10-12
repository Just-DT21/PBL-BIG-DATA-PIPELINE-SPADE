# How to publish these changes (web-based workflow)

The fix is finished and tested — it just has not been pushed, because this
environment has no GitHub credentials (`git push` fails with
`could not read Username for 'https://github.com'`). Nothing here is deployed
yet. Pick **either** route below.

The files in this folder are byte-identical to the ones that passed the full
test suite (134 tests), so uploading them reproduces exactly what was verified.

---

## Route A — push the prepared clone (one command, fastest)

A clone of your repository is already committed and waiting at:

```
C:\Users\ADMIN\AppData\Local\Temp\pbl-repo
```

It contains two new commits on `main`, and `origin` already points at your repo:

```
99588a8  Restore the missing worker module so file processing works in production
239f79d  Add a collapsible sidebar that remembers its state per browser
```

Open a terminal and run:

```bash
cd "C:\Users\ADMIN\AppData\Local\Temp\pbl-repo"
git push origin main
```

Git Credential Manager will ask you to sign in to GitHub the first time. Your
own credentials work; this environment deliberately has none.

> Note: the temp folder can be cleared by Windows. If it is gone, use
> `nami-worker-fix-and-sidebar.patch` from this folder instead
> (`git am < nami-worker-fix-and-sidebar.patch` in a clone), or use Route B.

---

## Route B — publish through the GitHub website

**Do not** use *Upload files* to drop the whole `github-upload` folder in: that
would add a stray `github-upload/` directory to your repository. Upload the
**contents** — the `spade`, `static` and `tests` folders and the `.gitignore`
file — or follow the paste steps below, which are the most reliable.

### B1. Restore the two missing files (this is what fixes production)

These two files are absent from the deployed branch, which is why every job
dies with `No module named spade.worker`.

1. Go to <https://github.com/Just-DT21/PBL-BIG-DATA-PIPELINE-SPADE>
2. **Add file → Create new file**
3. In the filename box type exactly: `spade/worker.py`
4. Paste the contents of `spade/worker.py` from this folder (open it with any
   text editor; it is the file you just downloaded).
5. Scroll down, set the commit message to
   `Restore the missing worker module`, and click **Commit changes**.
6. Repeat for the second file: **Add file → Create new file**, name it exactly
   `spade/__init__.py`, paste the contents of `spade/__init__.py` (three lines),
   and commit.

**Paste the file contents directly — do not retype them.** A typo in
`spade/worker.py` will produce a different failure instead of a fix.

At this point production works again: uploads, analysis and optimisation. You
can verify with the smoke test at the bottom of this page before continuing.

### B2. Add the `.gitignore` (keeps private data and bytecode out)

1. **Add file → Create new file**, name it exactly `.gitignore`
   (the leading dot matters; GitHub accepts the name).
2. Paste the contents of `.gitignore` from this folder, and commit.

Without this file, `data/`, `logs/`, `.env` and `__pycache__` can be committed
by accident — that is how 30 compiled `.pyc` files ended up in the repository.

### B3. The collapsible sidebar (three existing files)

For each file below: open it in your repository, click the **pencil** (Edit)
icon, select all the existing content, paste the new content from this folder,
then **Commit changes**.

| File in your repository | Replace with |
| --- | --- |
| `static/index.html`        | `static/index.html` from this folder |
| `static/css/app.css`       | `static/css/app.css` from this folder |
| `static/js/app.js`         | `static/js/app.js` from this folder |

Select-all-then-paste is safe here: each file in this folder is the complete
file, and all three go together. If you paste only one or two of them the
sidebar will half-work (for example the button will appear but do nothing).

### B4. The tests (optional, but recommended)

Create `tests/test_worker_contract.py` and `tests/test_shell_and_sidebar.py`
the same way as B1. They are not needed for production, but they are what
catches this class of outage next time.

### B5. Optional — remove the committed bytecode

Only possible from a command line; skip it if you are working in the browser:

```bash
git rm -r --cached $(git ls-files | grep __pycache__)
git commit -m "Stop tracking compiled bytecode"
```

---

## After pushing: Render

Render redeploys automatically on a push to `main`. Watch the deploy finish in
your Render dashboard (**Events** tab shows the new commit hash). The commit it
should build is `99588a8` (or `239f79d` if you applied both).

Then confirm the deployed commit is the one you pushed:

```bash
git ls-remote --heads https://github.com/Just-DT21/PBL-BIG-DATA-PIPELINE-SPADE
```

## Smoke test on the live site

Sign in, then upload a CSV. The File Analysis step must reach **analyzed**
instead of `analysis_failed`.

The failure looks like this (this is the real response from your production
site, captured before the fix):

```
status: analysis_failed
error:  The processing worker exited without producing a result (exit code 1).
        Reported: /opt/render/project/src/.venv/bin/python: No module named spade.worker
```

After the fix the same upload reports `analyzed` with a row and column count.
Then run the optimisation and download the output, and collapse/expand the
sidebar to confirm the rail works on the deployed origin.

## What was verified, and what was not

* Verified locally: 134 tests pass, including 7 new ones that assert the worker
  module exists, loads in a fresh interpreter, and completes real analysis and
  optimisation jobs in a subprocess.
* Verified in a clone of your repository: the same 134 tests pass on the exact
  tree these files come from, and the new worker tests fail if `spade/worker.py`
  is removed (checked deliberately).
* Verified in a browser: the sidebar collapses to a 64px rail, remembers the
  choice, expands again, and behaves as a drawer on a 390px viewport.
* **Not verified: the Render deployment and the live site after the fix**,
  because the push could not be made from here. Everything above is local or
  repository-level evidence only.
