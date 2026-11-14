#!/usr/bin/env bash
# Ubuntu's FFmpeg is not guaranteed to include libvmaf; benchmark evaluation needs it.
set -euo pipefail

: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
: "${GITHUB_PATH:?GITHUB_PATH is required}"
: "${GH_TOKEN:?GH_TOKEN is required for the release metadata API}"

archive="$RUNNER_TEMP/vmaf-ffmpeg.tar.xz"
destination="$RUNNER_TEMP/vmaf-ffmpeg"
asset="ffmpeg-master-latest-linux64-gpl.tar.xz"
repository="FFmpeg-Builds/FFmpeg-Builds"

checksum=$(gh api "repos/$repository/releases/latest" \
  --jq '.assets[] | select(.name == "ffmpeg-master-latest-linux64-gpl.tar.xz") | .digest')
checksum="${checksum#sha256:}"
if [[ ! "$checksum" =~ ^[0-9a-f]{64}$ ]]; then
  echo "The libvmaf-enabled FFmpeg release has no valid SHA-256 digest."
  exit 1
fi

mkdir -p "$destination"
gh release download latest --repo "$repository" --pattern "$asset" \
  --dir "$RUNNER_TEMP" --clobber
printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check
tar --extract --xz --file "$archive" --directory "$destination" --strip-components=1
"$destination/bin/ffmpeg" -hide_banner -filters 2>/dev/null | grep -q libvmaf
"$destination/bin/ffmpeg" -version | head -n 1
printf '%s\n' "$destination/bin:$PATH" >> "$GITHUB_PATH"
