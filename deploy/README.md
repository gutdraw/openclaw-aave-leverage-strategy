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
  --journal trades.sqlite3
```

Do not run the service and the previous `screen` process at the same time. The
existing `.lock` file is still the single-instance guard, but two processes must
not compete for a wallet even if their state files differ.
