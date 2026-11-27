#!/bin/bash
# ============================================================
# 🛡️ MovieBoxBDStreamBot — 24/7 KEEP-ALIVE SUPERVISOR (VPS-এর জন্য)
# ============================================================
# Koyeb/Render/Docker-এ এই ফাইল লাগবে না — ওখানে Docker নিজেই
# প্রসেস মরলে অটো রিস্টার্ট করে।
#
# শুধু VPS/লোকাল মেশিনে চালাতে:
#   chmod +x start.sh
#   ./start.sh
#
# কাজ:
#   1. bot.py চালু করে
#   2. প্রসেস যেকোনো কারণে মরলে ৩ সেকেন্ড পর আবার চালু করে (২৪/৭)
#   3. selfheal.py চালু থাকায় RAM 95% হলে বট নিজেই fresh রিলোড
#      নেয় (exec) — তখন এই লুপ চালু থাকেই, কিছু করতে হয় না
# ============================================================

cd "$(dirname "$0")"

echo "🛡️ [KEEP-ALIVE] 24/7 supervisor started — bot will never stay down"

while true; do
    python3 bot.py
    CODE=$?
    echo "⚠️ [KEEP-ALIVE] bot exited (code $CODE) — restarting in 3 seconds..."
    sleep 3
done
