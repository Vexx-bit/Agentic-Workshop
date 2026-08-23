# Landing page

Static. No build step, no framework, no environment variables, no secrets on the
page. One file plus this README.

## Deploy

Vercel > New Project > import this repo > set **Root Directory** to `web` >
Framework Preset **Other** > Deploy.

Keeping it as a subdirectory of the same repo is the right call: the judges see
one commit history, and the page never drifts out of step with the agent.

## Configure

Edit the `CONFIG` object at the bottom of `index.html`:

| Field | Value |
|---|---|
| `waNumber` | The Twilio WhatsApp sandbox number, digits only, no `+` |
| `joinCode` | The exact sandbox join phrase, e.g. `join blue-tiger` |
| `telegram` | Leave empty until a Telegram adapter exists |

The WhatsApp deep link is assembled in JavaScript from those values, so the
number and code appear once in the file.

## Deliberate omissions

- **No API calls.** The page never talks to Cloud Run, so it cannot leak a
  service URL, a token, or a student's data, and it cannot go down with the
  backend.
- **No analytics.** Nothing to disclose in a privacy section.
- **No Telegram button** until the adapter is real. A dead button in a demo is
  worse than an honest "planned" chip.
