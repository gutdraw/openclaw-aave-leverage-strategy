# Supervised runtime

The service templates replace the long-running `screen` process with systemd.
They assume the EC2 checkout is at `/home/ec2-user/skunk/openclaw-aave-leverage-strategy`
and that the existing `my-config.yml` and `.venv` remain in that directory.

Install only after a read-only reconciliation confirms the current wallet state:

```bash
sudo cp deploy/openclaw-aave-leverage-strategy.service /etc/systemd/system/
sudo cp deploy/openclaw-aave-leverage-strategy-health.service /etc/systemd/system/
sudo cp deploy/openclaw-aave-leverage-strategy-health.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-aave-leverage-strategy.service
sudo systemctl enable --now openclaw-aave-leverage-strategy-health.timer
```

Verify the service and journal before allowing live execution:

```bash
systemctl status openclaw-aave-leverage-strategy.service
journalctl -u openclaw-aave-leverage-strategy.service -f
python scripts/check_health.py \
  --heartbeat trades.heartbeat.json \
  --journal trades.sqlite3 \
  --config my-config.yml \
  --alerts trades.alerts.json
```

The health check also compares a live heartbeat's health factor with the
configured reduce/close thresholds. It writes durable active/resolved alert
state to `trades.alerts.json` and emits transition messages to the systemd
journal. Any active alert makes the health-check unit non-zero so ordinary
systemd monitoring can surface it; critical alerts use a distinct exit code.
With `--config`, the same five-minute health run performs an independent,
read-only Aave account-risk probe and writes `trades.risk.json` atomically. It
warns at direct health factor 1.14, escalates at 1.12, and raises critical
alerts if the probe is unavailable or the snapshot is older than ten minutes.
The heartbeat also records the selected funding provider and sanitized source
failure labels. The checker raises warning alerts for
`funding_rate_unavailable` and `market_data_degraded` when those signals are
present.
No trading decision is changed by the checker.

Do not run the service and the previous `screen` process at the same time. The
existing `.lock` file is still the single-instance guard, but two processes must
not compete for a wallet even if their state files differ.
