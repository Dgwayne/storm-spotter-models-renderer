#!/bin/bash
# Self-test for model_config.sh. Run it from anywhere:
#   bash scripts/lib/model_config_selftest.sh
# It only reads config/products.yml and works in /tmp; it touches nothing
# in production and uploads nothing. Needs yq on PATH (source stp-env.sh
# first on the render boxes).
set -uo pipefail
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${LIB_DIR}/../.." && pwd)"
LIB="${LIB_DIR}/model_config.sh"
CONFIG="${REPO_ROOT}/config/products.yml"
cd "${REPO_ROOT}" || exit 1
command -v yq >/dev/null || { echo "yq not on PATH; source stp-env.sh first"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
pass=0; fail=0
ck() { if [ "$2" = "$3" ]; then echo "  PASS $1"; pass=$((pass+1)); else echo "  FAIL $1: got [$2] want [$3]"; fail=$((fail+1)); fi; }

echo "== 1. real read returns the full list =="
ck "45 HRRR products" "$( . "$LIB"; load_model_products HRRR "$CONFIG"; printf '%s' "$PRODUCTS" | grep -c . )" "$(yq -r '.models.HRRR.products | length' "$CONFIG")"
ck "the codes the short reads used to reject are present" "$( . "$LIB"; load_model_products HRRR "$CONFIG"; printf '%s\n' "$PRODUCTS" | grep -cx 'mslp\|ehi1\|mlcin\|precipTotal' )" "4"
ck "a different model reads its own list" "$( . "$LIB"; load_model_products NAM "$CONFIG"; printf '%s' "$PRODUCTS" | grep -c . )" "$(yq -r '.models.NAM.products | length' "$CONFIG")"

echo "== 2. custom target variable =="
ck "ALL_PRODUCTS populated" "$( . "$LIB"; load_model_products HRRR "$CONFIG" ALL_PRODUCTS; printf '%s' "$ALL_PRODUCTS" | grep -c . )" "$(yq -r '.models.HRRR.products | length' "$CONFIG")"

echo "== 3. the exported list short-circuits and makes ZERO yq calls =="
mkdir -p "${WORK}/bin"
printf '#!/bin/bash\ntouch %s/yq_was_called\nexit 1\n' "${WORK}" > "${WORK}/bin/yq"; chmod +x "${WORK}/bin/yq"
ck "uses the exported list" "$( export PATH="${WORK}/bin:$PATH" MODEL_PRODUCTS=$'aaa\nbbb\nccc' MODEL_PRODUCTS_FOR=HRRR; . "$LIB"; load_model_products HRRR "$CONFIG"; printf '%s' "$PRODUCTS" | grep -c . )" "3"
ck "yq never invoked" "$([ -f "${WORK}/yq_was_called" ] && echo called || echo no)" "no"

echo "== 4. a list exported for ANOTHER model is refused =="
ck "HRRR ignores a list exported for NAM" "$( export MODEL_PRODUCTS=$'aaa\nbbb\nccc' MODEL_PRODUCTS_FOR=NAM; . "$LIB"; load_model_products HRRR "$CONFIG"; printf '%s' "$PRODUCTS" | grep -c . )" "$(yq -r '.models.HRRR.products | length' "$CONFIG")"

echo "== 5. a PARTIAL read is rejected (the bug this lib exists for) =="
cat > "${WORK}/bin/yq" <<'MOCK'
#!/bin/bash
case "$1$2$3" in
  *length*) echo 45 ;;
  *) printf 'refc\nrefd1km\nt2m\ntd2m\nwind10m\ngust10m\n' ;;
esac
MOCK
chmod +x "${WORK}/bin/yq"
out=$( export PATH="${WORK}/bin:$PATH"; unset MODEL_PRODUCTS MODEL_PRODUCTS_FOR; . "$LIB"; load_model_products HRRR "$CONFIG" 2>&1; echo "RC=$?" )
ck "partial read is FATAL" "$(printf '%s' "$out" | grep -c FATAL)" "1"
ck "warns with the real counts, 5 attempts" "$(printf '%s' "$out" | grep -c 'read 6/45 entries')" "5"
ck "returns non-zero" "$(printf '%s' "$out" | grep -c 'RC=1')" "1"

echo "== 6. under 'set -e' a bad read still aborts the caller =="
printf '#!/bin/bash\nset -euo pipefail\n. %s\nload_model_products HRRR %s\necho REACHED-THE-RENDER\n' "$LIB" "$CONFIG" > "${WORK}/fake_render.sh"
out6=$( export PATH="${WORK}/bin:$PATH"; unset MODEL_PRODUCTS MODEL_PRODUCTS_FOR; bash "${WORK}/fake_render.sh" 2>&1; echo "RC=$?" )
ck "caller aborted before rendering" "$(printf '%s' "$out6" | grep -c 'REACHED-THE-RENDER')" "0"
ck "caller exited non-zero" "$(printf '%s' "$out6" | grep -c 'RC=1')" "1"

echo "== 7. what the guard this replaced did with the same partial read =="
ck "old guard silently accepted it" "$( export PATH="${WORK}/bin:$PATH"
  P=$(yq -r ".models.HRRR.products[]" "$CONFIG" 2>/dev/null || true)
  if [ -z "$P" ]; then echo "rejected"; else echo "ACCEPTED-$(printf '%s' "$P" | grep -c .)"; fi )" "ACCEPTED-6"

echo "== 8. filter_products: all-valid codes pass through =="
ck "filtered list is exactly the filter" "$( . "$LIB"; load_model_products HRRR "$CONFIG"; filter_products HRRR "refc t2m mslp" "$CONFIG" 2>/dev/null; printf '%s' "$PRODUCTS" | tr '
' ' ' )" "refc t2m mslp "

echo "== 9. filter_products: a code the in-hand list lost but the config HAS =="
out9=$( . "$LIB"; PRODUCTS=$'refc
t2m'; filter_products HRRR "refc t2m mslp" "$CONFIG" 2>&1 >/dev/null; )
ck "warns instead of dying" "$(printf '%s' "$out9" | grep -c "transient, continuing")" "1"
ck "warn names the entry count" "$(printf '%s' "$out9" | grep -c "2 entries")" "1"
ck "the code is kept, group survives" "$( . "$LIB"; PRODUCTS=$'refc
t2m'; filter_products HRRR "refc t2m mslp" "$CONFIG" 2>/dev/null; printf '%s' "$PRODUCTS" | tr '
' ' ' )" "refc t2m mslp "

echo "== 10. filter_products: a code in NEITHER is still a hard error =="
out10=$( . "$LIB"; load_model_products HRRR "$CONFIG"; filter_products HRRR "refc notaproduct" "$CONFIG" 2>&1 >/dev/null; echo "RC=$?" )
ck "typo still fails loudly" "$(printf '%s' "$out10" | grep -c "ERROR: PRODUCT_FILTER contains 'notaproduct'")" "1"
ck "returns non-zero" "$(printf '%s' "$out10" | grep -c 'RC=1')" "1"

echo "== 11. exact-line matching, no substring false positives =="
ck "refc does not match refc1km" "$( . "$LIB"; PRODUCTS=$'refc1km
t2m'; filter_products HRRR "refc" "/nonexistent" 2>/dev/null; echo "rc=$?" )" "rc=1"

echo
echo "RESULT pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
