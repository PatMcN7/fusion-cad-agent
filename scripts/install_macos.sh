#!/bin/zsh
set -euo pipefail

script_dir="${0:A:h}"
repo_dir="${script_dir:h}"
addin_root="$HOME/Library/Application Support/Autodesk/FusionAddins"
link_path="$addin_root/FusionCadAgent"

mkdir -p "$addin_root"

if [[ -e "$link_path" || -L "$link_path" ]]; then
  current_target="$(readlink "$link_path" 2>/dev/null || true)"
  if [[ "$current_target" == "$repo_dir" ]]; then
    print "FusionCadAgent is already linked: $link_path"
    exit 0
  fi
  print -u2 "Refusing to replace existing path: $link_path"
  exit 1
fi

ln -s "$repo_dir" "$link_path"
print "Linked FusionCadAgent into Fusion: $link_path"
print "In Fusion, open Utilities > Scripts and Add-Ins, select FusionCadAgent, then click Run."

