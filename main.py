import os
import requests

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

response = requests.post(
    WEBHOOK_URL,
    json={
        "username": "华尔街见闻快讯",
        "content": "测试成功：GitHub Actions → Discord Webhook 正常。",
    },
    timeout=20,
)

print("Discord status:", response.status_code)
print(response.text)
response.raise_for_status()
