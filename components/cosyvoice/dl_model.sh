#!/bin/bash
# 从 ModelScope 直链下载 CosyVoice2-0.5B 权重，支持续传。
set -u
cd "D:/WorkBuddy/QQAgent/cosyvoice2/CosyVoice2-0.5B" || exit 1
BASE="https://www.modelscope.cn/api/v1/models/iic/CosyVoice2-0.5B/repo?Revision=master&FilePath="

FILES="llm.pt flow.pt hift.pt campplus.onnx speech_tokenizer_v2.onnx cosyvoice2.yaml configuration.json CosyVoice-BlankEN/config.json CosyVoice-BlankEN/generation_config.json CosyVoice-BlankEN/merges.txt CosyVoice-BlankEN/tokenizer_config.json CosyVoice-BlankEN/vocab.json CosyVoice-BlankEN/model.safetensors"

for f in $FILES; do
  enc=$(printf '%s' "$f" | sed 's|/|%2F|g')
  mkdir -p "$(dirname "$f")"
  code=$(curl -L -C - --retry 5 --retry-delay 3 -s -o "$f" -w "%{http_code}" "$BASE$enc")
  sz=$(stat -c %s "$f" 2>/dev/null || echo 0)
  echo "$code  ${sz}B  $f"
done
echo "ALL_DONE"
