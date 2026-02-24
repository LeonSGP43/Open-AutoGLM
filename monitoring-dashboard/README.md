# Monitoring Dashboard

Local metrics dashboard for Open-AutoGLM runs.

## Run

```bash
cd monitoring-dashboard
npm run dev
```

Open `http://127.0.0.1:5173`.

## Notes

- `npm run dev` will:
  - regenerate metrics JSON/Markdown into `monitoring-dashboard/data/`
  - regenerate HTML into `monitoring-dashboard/public/metrics_dashboard.html`
  - start a local static server with periodic refresh (default every 30s)
- Dashboard supports i18n:
  - click `English / 中文` in the header, or use `?lang=en` / `?lang=zh`
- Dashboard includes metric dictionary and case details:
  - each metric explains data meaning and source tables/logs
  - recent case table shows concrete run samples (time/task/failure counts/tokens/log file)
  - case table supports live search and status filtering (finished/failed/unknown)
- Override runtime settings with env vars:
  - `PORT` (default `5173`)
  - `REFRESH_INTERVAL_MS` (default `30000`)
  - `PYTHON_BIN` (default `python3`)
