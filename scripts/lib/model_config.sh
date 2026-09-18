# shellcheck shell=bash
# model_config.sh - one concurrency-safe read of a model's product list.
#
# WHY THIS EXISTS. The fan-out runners (run-model.sh on the render boxes, the
# matrix jobs in the Actions workflows) start one process per render group,
# and each one used to read config/products.yml at the same instant. Under
# load that read comes back SHORT: not empty, just missing entries. A short
# list makes the PRODUCT_FILTER check reject a perfectly valid code with
# "not in models.<M>.products" and kill that group for the whole tick.
# Measured on box 1 2026-09-18: 334 lost group-ticks in three days across
# HRRR, RAP, RRFS, GFS, NAM and HREFPROB, worst on HRRR's core group (refc,
# t2m, td2m, wind10m, gust10m, mslp, sfccape, precip1h). The guard this
# replaces only caught a FULLY EMPTY read, so every partial one sailed past
# it and reported a perfectly good config as wrong.
#
# Two defences, in order:
#   1. MODEL_PRODUCTS in the environment wins, but ONLY when
#      MODEL_PRODUCTS_FOR names the model being asked for. A fan-out runner
#      reads the list ONCE per tick and exports both, so N groups share one
#      read instead of racing N. The paired name means a list exported for
#      one model can never be handed to a different one: a mismatch falls
#      through to a fresh validated read rather than rendering the wrong
#      product set. Unset MODEL_PRODUCTS to force a fresh read.
#   2. Otherwise read it here and CHECK THE COUNT against the list's own
#      declared length. A read that disagrees with itself is retried; a
#      disagreement that survives every attempt is fatal and loud. "I could
#      not read the config" must never masquerade as "your config is wrong".
#
# Usage: load_model_products <MODEL> <CONFIG> [VARNAME]   (VARNAME: PRODUCTS)

load_model_products() {
  local _model="$1" _config="$2" _var="${3:-PRODUCTS}"
  local _want="" _got="" _try _list

  if [ -n "${MODEL_PRODUCTS:-}" ] && [ "${MODEL_PRODUCTS_FOR:-}" = "${_model}" ]; then
    printf -v "${_var}" '%s' "${MODEL_PRODUCTS}"
    return 0
  fi

  for _try in 1 2 3 4 5; do
    _want=$(yq -r ".models.${_model}.products | length" "$_config" 2>/dev/null || true)
    _list=$(yq -r ".models.${_model}.products[]" "$_config" 2>/dev/null || true)
    _got=$(printf '%s' "${_list}" | grep -c . || true)
    if [ -n "${_want}" ] && [ "${_want}" -gt 0 ] 2>/dev/null && [ "${_got}" = "${_want}" ]; then
      printf -v "${_var}" '%s' "${_list}"
      return 0
    fi
    echo "WARN: models.${_model}.products read ${_got:-0}/${_want:-?} entries (attempt ${_try}/5); retrying" >&2
    sleep 2
  done

  echo "FATAL: could not read models.${_model}.products from ${_config} consistently after 5 attempts (last ${_got:-0}/${_want:-?} entries)" >&2
  return 1
}

# Apply PRODUCT_FILTER to PRODUCTS.
#
# WHY THIS IS NOT JUST A LOOP WITH grep. On 2026-09-18 the render groups were
# rejecting codes that ARE in config/products.yml, and the codes MOVED from
# tick to tick: mslp/ehi1/mlcin/precipTotal one tick, wind10m/uh25/shear1/mcc/
# snod another, scattered through the list rather than truncated off its end.
# Two hypotheses were tested on box 1 under real load (~25) and BOTH failed:
# the config reads were clean 40/40, and the old `printf | grep` membership
# test gave 0 false rejections in 2000 runs. So the mechanism is still unknown.
#
# What is certain is the COST: one unexplained miss killed a whole render
# group for that tick. So before failing, re-verify the code against the
# config itself. If the config has it, the in-hand list was wrong rather than
# the config, and that is a transient to warn about and ride through, not a
# reason to drop 8 products for 10 minutes. A code that is genuinely absent
# from the config is still a hard error, which is what the check was for: a
# typo in a render group must fail loudly. The WARN carries the entry count
# and MODEL_PRODUCTS_FOR so the next occurrence identifies itself.
filter_products() {
  local _model="$1" _filter="$2" _config="${3:-}" _p _l _filtered="" _n _fresh
  local _hay=$'\n'"${PRODUCTS}"$'\n'
  for _p in ${_filter}; do
    case "${_hay}" in
      *$'\n'"${_p}"$'\n'*) _filtered="${_filtered}${_p}"$'\n'; continue ;;
    esac
    _n=0
    while IFS= read -r _l; do [ -n "${_l}" ] && _n=$((_n+1)); done <<< "${PRODUCTS}"
    _fresh=0
    if [ -n "${_config}" ]; then
      _fresh=$(yq -r ".models.${_model}.products[]" "${_config}" 2>/dev/null | grep -cx -- "${_p}" 2>/dev/null || true)
    fi
    if [ "${_fresh:-0}" -ge 1 ] 2>/dev/null; then
      echo "WARN: '${_p}' missing from the in-hand ${_model} list (${_n} entries, MODEL_PRODUCTS_FOR=${MODEL_PRODUCTS_FOR:-unset}) but present in ${_config}; transient, continuing" >&2
      _filtered="${_filtered}${_p}"$'\n'
      continue
    fi
    echo "ERROR: PRODUCT_FILTER contains '${_p}', not in models.${_model}.products (in-hand list had ${_n} entries, MODEL_PRODUCTS_FOR=${MODEL_PRODUCTS_FOR:-unset})" >&2
    return 1
  done
  PRODUCTS="${_filtered}"
  return 0
}
