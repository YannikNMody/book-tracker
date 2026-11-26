# Audit

Before publishing, this repository was audited for public release with an AI coding assistant (Claude), using the course audit prompt word for word. The audit is read-only: it prints no secret values, changes no files and runs no project code.

| Pass | Date | State audited |
|---|---|---|
| 1 | 24 Nov 2026 | `main` as finished after building (10 commits, full history), plus the GitHub Actions run logs and a read-only check of the live database |
| 2 | 26 Nov 2026 | `submission-prep` after the fixes below |

Both passes ran on a fresh clone of the GitHub repository, so files that exist only on the author's computer (such as `.env`) were not visible to them.

## What was checked

- **Secrets and credentials:** every commit in the history, searched for API keys, Supabase keys, tokens, passwords, private keys and database connection strings. Every connection string was checked with its password part masked.
- **GitHub Actions logs:** these become public with the repository; searched for connection strings and secrets.
- **Database access:** the access rules in the migrations, and the live database (read-only): which tables the public key can read, whether it can write, and Supabase's security advisor.
- **Personal data:** commit metadata, local paths, screenshots (content and image metadata).
- **Files that should not be published:** `.env`, scraper output, caches, virtual environments, files deleted from history.
- **`.gitignore`:** whether the rules cover the files above, and whether any tracked file is matched by them.
- **Reproducibility:** dependencies, setup instructions, configuration examples.
- **README:** the seven required topics and a screenshot.

## Pass 1 findings and what was done

No secrets were found in the current files, the history or the Actions logs. Every connection string pointing at the real database contains only a password placeholder; the others point at fake test hosts or the throwaway database used in CI. No history rewrite was needed.

The live database matched the migrations: the public (publishable) key can only read the catalogue, history and run tables, cannot read the error and staging tables, cannot write and cannot run the merge function. Row-level security is on for every table. Supabase's security advisor reported one informational item, "RLS enabled, no policy" on the two internal tables, which is intended.

| # | Finding | Decision | Fixed in |
|---|---|---|---|
| C1 | The README predates the submission requirements: no User interaction, Learning or Limitations and assistance sections, no mention of AI tools. | **Fixed.** Restructured into the seven sections; Learning added; AI use and other sources disclosed. | `ed23a31`, `3238775` |
| C2 | Setup was incomplete: no virtual environment or install steps in the main README, no instructions for running the pipeline against your own database. | **Fixed.** Two options: view the data with no setup, or run everything with your own Supabase project. | `ed23a31` |
| R1 | `.env.example` showed this project's database host and project reference (password was a placeholder). Not a secret, but someone copying it would point at the author's database. | **Fixed.** Generic placeholders; outdated password advice corrected; unused Node rules removed from `.gitignore`. | `ec7d597` |
| R2 | The app ships with the Supabase URL and publishable key built in. | **Kept, documented.** This key is public by design and limited to reading (verified on the live database). The README explains the choice. | `ed23a31` |
| R3 | The `details.png` screenshot showed a book description repeated twice (probably taken before the display fix). | **Fixed.** Screenshot retaken. | `ed23a31` |
| R4 | Dependencies only set minimum versions. | **Fixed** with a "tested with" line, using the versions the GitHub Actions run installed. | `ed23a31` |
| R5 | No LICENSE file. | **Decision, no file.** Coursework published for grading; the README states "no license, all rights reserved". | `ed23a31` |
| — | Free Supabase projects pause after about a week of low activity, so the app may show a connection error when opened later. | **Accepted.** The professor confirmed this is not an issue for grading. The README explains it and the screenshots show the app with data. | `85e9207` |
| — | The author's email address is in every commit and becomes public. | **Accepted, no action.** Not a secret; removing it would mean rewriting the history. | — |

## Pass 2 result

All changes since pass 1 were checked again: no secrets (every match was README text or the placeholder connection string), no tracked file matched by `.gitignore`, no metadata in the screenshots, all seven README sections present. The only confirmed issue was that the README linked to this file before it existed. The test suites passed on GitHub Actions for the audited commit.

## Remaining limitations

- Files that exist only on the author's computer (`.env`, `scraper/output/`) were not inspected; the `.gitignore` rules exclude them.
- The database password lives in `.env` and as a GitHub Actions secret; neither is in the repository. Repository settings on GitHub (secrets, collaborators) cannot be seen from a clone.
- The audits did not run the scraper or the app; the automated tests and the author's manual check did.
- An audit that finds nothing does not prove the project is safe.
