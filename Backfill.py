name: Backfill Missed News (manual)

# 只能手动触发，绝不会自己运行，也不影响正在跑的三个定时 workflow。
on:
  workflow_dispatch:
    inputs:
      hours:
        description: "补发最近多少小时内的快讯"
        required: true
        default: "6"
      channels:
        description: "补发哪些频道（逗号分隔：us,a,hk）"
        required: true
        default: "us,a,hk"
      dry_run:
        description: "试运行：只列出会补发哪些，不实际发送"
        type: boolean
        required: true
        default: true

concurrency:
  group: wallstreetcn-backfill
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  backfill:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install requests

      - name: Backfill
        env:
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
          DISCORD_WEBHOOK_URL_A: ${{ secrets.DISCORD_WEBHOOK_URL_A }}
          DISCORD_WEBHOOK_URL_HK: ${{ secrets.DISCORD_WEBHOOK_URL_HK }}
          HOURS: ${{ inputs.hours }}
          CHANNELS: ${{ inputs.channels }}
          DRY_RUN: ${{ inputs.dry_run }}
        run: python backfill.py

      - name: Save state
        if: ${{ inputs.dry_run == false }}
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add seen.json seen_a.json seen_hk.json
          git diff --cached --quiet && exit 0
          git commit -m "Backfill missed news state"
          for i in 1 2 3 4 5; do
            git pull --rebase origin main && git push && exit 0
            sleep $((RANDOM % 5 + 1))
          done
          exit 1
