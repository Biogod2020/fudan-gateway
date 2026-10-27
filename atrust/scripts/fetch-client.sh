#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
source_dir=${1:-"$root/.cache/zju-connect"}
commit=8de443f582d962554f8d86d53cdddf6a11ec82a6
if [ ! -d "$source_dir/.git" ]; then
    git clone --no-checkout https://github.com/Mythologyli/zju-connect.git "$source_dir"
    git -C "$source_dir" checkout --detach "$commit"
fi
test "$(git -C "$source_dir" rev-parse HEAD)" = "$commit" || {
    printf '%s\n' 'Client checkout differs from the pinned upstream commit.' >&2
    exit 1
}
test -z "$(git -C "$source_dir" status --porcelain)" || {
    printf '%s\n' 'Client checkout has local changes; preserve and review them first.' >&2
    exit 1
}
printf '%s\n' "$source_dir"
