#!/usr/bin/env bash
# Regenerate the real ffprobe fixtures used by the unit tests.
#
# Fixtures are committed so the test suite never needs ffmpeg; this script exists
# so they can be reproduced/refreshed deliberately. Handcrafted fixtures (marked
# as such in tests/data/README.md) are not produced here.
#
# Usage: scripts/generate_test_fixtures.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SAMPLES="$ROOT/samples"
DATA="$ROOT/tests/data/ffprobe"
mkdir -p "$SAMPLES" "$DATA"

ff() { ffmpeg -nostdin -hide_banner -v error -y "$@"; }
snap() { ffprobe -v error -print_format json -show_format -show_streams "$1" > "$DATA/$2"; echo "wrote $DATA/$2"; }

# A tiny SRT used by the samples.
SRT="$SAMPLES/fixture.srt"
printf '1\n00:00:00,500 --> 00:00:02,000\nfixture line one\n\n2\n00:00:02,200 --> 00:00:03,800\nfixture line two\n\n' > "$SRT"

# 1) 1080p24 H.264 High@4.1 + AC-3 5.1 (eng) + AC-3 2.0 (jpn) + SubRip (eng)
ff -f lavfi -i "testsrc2=size=1920x1080:rate=24000/1001:duration=4" \
   -f lavfi -i "sine=frequency=440:sample_rate=48000:duration=4" \
   -f lavfi -i "sine=frequency=880:sample_rate=48000:duration=4" \
   -f srt -i "$SRT" \
   -map 0:v -map 1:a -map 2:a -map 3:s \
   -c:v libx264 -preset veryfast -profile:v high -level 4.1 -pix_fmt yuv420p -b:v 2000k -g 48 \
   -c:a ac3 -b:a:0 448k -ac:a:0 6 -b:a:1 192k -ac:a:1 2 \
   -c:s srt \
   -metadata:s:a:0 language=eng -metadata:s:a:0 title="English 5.1" \
   -metadata:s:a:1 language=jpn -metadata:s:a:1 title="Japanese 2.0" \
   -metadata:s:s:0 language=eng -metadata:s:s:0 title="English full" \
   "$SAMPLES/fixture_1080p.mkv"
snap "$SAMPLES/fixture_1080p.mkv" mkv_1080p_h264_ac3x2_srt.json

# 2) 720p60 H.264 + AAC stereo + SubRip (tolerated-but-not-AVCHD-legal cases)
ff -f lavfi -i "testsrc2=size=1280x720:rate=60000/1001:duration=3" \
   -f lavfi -i "sine=frequency=440:sample_rate=48000:duration=3" \
   -f srt -i "$SRT" \
   -map 0:v -map 1:a -map 2:s \
   -c:v libx264 -preset veryfast -profile:v high -level 4.0 -pix_fmt yuv420p -b:v 1500k -g 60 \
   -c:a aac -b:a 192k -ac 2 -c:s srt \
   -metadata:s:a:0 language=eng -metadata:s:s:0 language=eng \
   "$SAMPLES/fixture_720p.mkv"
snap "$SAMPLES/fixture_720p.mkv" mkv_720p_h264_aac_srt.json

# 3) 1080p HEVC — must be rejected by validation
ff -f lavfi -i "testsrc2=size=1920x1080:rate=24000/1001:duration=2" \
   -map 0:v -c:v libx265 -preset ultrafast -x265-params log-level=error -pix_fmt yuv420p \
   -tag:v hvc1 "$SAMPLES/fixture_hevc.mkv"
snap "$SAMPLES/fixture_hevc.mkv" mkv_1080p_hevc.json

# 4) MPEG-TS with H.264 + DTS (transport-stream input path)
ff -f lavfi -i "testsrc2=size=1920x1080:rate=24000/1001:duration=3" \
   -f lavfi -i "sine=frequency=440:sample_rate=48000:duration=3" \
   -map 0:v -map 1:a \
   -c:v libx264 -preset veryfast -profile:v high -level 4.1 -pix_fmt yuv420p -b:v 1800k -g 48 \
   -c:a dts -strict -2 -ac 6 -b:a 768k \
   -mpegts_flags +resend_headers \
   "$SAMPLES/fixture_1080p.ts"
snap "$SAMPLES/fixture_1080p.ts" ts_1080p_h264_dts.json

echo "done"
