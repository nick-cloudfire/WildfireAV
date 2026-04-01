#!/bin/bash
set -euo pipefail

CALL_DIR="$(pwd)"
TOA_DIR="$CALL_DIR/toas"
LOG_DIR="$CALL_DIR/logs"
mkdir -p "$TOA_DIR" "$LOG_DIR"

PYTHON_EXE="${PYTHON_EXE:-$HOME/miniforge3/envs/autoValidate/bin/python}"
CONDA_INIT="${CONDA_INIT:-$HOME/miniconda3/etc/profile.d/conda.sh}"
CONDA_ENV="${CONDA_ENV:-autoValidate}"
PARTITION="${PARTITION:-normal}"
WALLTIME="${WALLTIME:-24:00:00}"
SBATCH_MEM="${SBATCH_MEM:-0}"

echo "Running setupPipeline.py from: $CALL_DIR"
echo "TOA output dir: $TOA_DIR"
echo

if [ -f "$CONDA_INIT" ]; then
    source "$CONDA_INIT"
    conda activate "$CONDA_ENV"
fi

echo "[1/4] Running setupPipeline.py"
"$PYTHON_EXE" setupPipeline.py

echo
echo "[2/4] Reading FIRE_ROOT_LOGIN_NODE from pipelineConfig.py"
FIRE_ROOT_LOGIN_NODE="$("$PYTHON_EXE" - <<'PY'
import pipelineConfig
print(pipelineConfig.FIRE_ROOT_LOGIN_NODE)
PY
)"

if [ ! -d "$FIRE_ROOT_LOGIN_NODE" ]; then
    echo "ERROR: FIRE_ROOT_LOGIN_NODE does not exist: $FIRE_ROOT_LOGIN_NODE"
    exit 1
fi

CASE_LIST="$CALL_DIR/case_list.txt"
find "$FIRE_ROOT_LOGIN_NODE" -mindepth 1 -maxdepth 1 -type d -printf "%f\n" \
    | grep -E '^[0-9]+$' \
    | sort -n \
    | awk -v root="$FIRE_ROOT_LOGIN_NODE" '{print root "/" $0}' > "$CASE_LIST"

NCASES="$(wc -l < "$CASE_LIST" | tr -d ' ')"
if [ "$NCASES" -eq 0 ]; then
    echo "ERROR: No numeric case folders found in $FIRE_ROOT_LOGIN_NODE"
    exit 1
fi

echo "Found $NCASES cases under $FIRE_ROOT_LOGIN_NODE"
echo "Case list written to $CASE_LIST"

readarray -t CFG < <("$PYTHON_EXE" - <<'PY'
import pipelineConfig
print(pipelineConfig.BASE_VALIDATION)
print(pipelineConfig.FIRE_ROOT_LOGIN_NODE)
print(pipelineConfig.FIRE_ROOT)
PY
)

BASE_VALIDATION="${CFG[0]}"
FIRE_ROOT_LOGIN_NODE="${CFG[1]}"
FIRE_ROOT="${CFG[2]}"

SLURM_SCRIPT="$CALL_DIR/run_parallel_cases.slurm"

cat > "$SLURM_SCRIPT" <<'SLURM'
#!/bin/bash
#SBATCH --job-name=elmfire_parallel
#SBATCH --nodes=1
#SBATCH --ntasks=96
#SBATCH --cpus-per-task=1
#SBATCH --time=__WALLTIME__
#SBATCH --partition=__PARTITION__
#SBATCH --mem=__SBATCH_MEM__
#SBATCH --output=__LOG_DIR__/slurm_%j.out
#SBATCH --error=__LOG_DIR__/slurm_%j.err

set -euo pipefail

CALL_DIR="__CALL_DIR__"
PYTHON_EXE="__PYTHON_EXE__"
CONDA_INIT="__CONDA_INIT__"
CONDA_ENV="__CONDA_ENV__"

BASE_VALIDATION="__BASE_VALIDATION__"
FIRE_ROOT_LOGIN_NODE="__FIRE_ROOT_LOGIN_NODE__"
FIRE_ROOT="__FIRE_ROOT__"

echo "============================================================"
echo "SLURM JOB START"
echo "JOB ID               : ${SLURM_JOB_ID:-unknown}"
echo "HOSTNAME             : $(hostname)"
echo "START TIME           : $(date)"
echo "CALL_DIR             : $CALL_DIR"
echo "PYTHON_EXE           : $PYTHON_EXE"
echo "CONDA_INIT           : $CONDA_INIT"
echo "CONDA_ENV            : $CONDA_ENV"
echo "BASE_VALIDATION      : $BASE_VALIDATION"
echo "FIRE_ROOT_LOGIN_NODE : $FIRE_ROOT_LOGIN_NODE"
echo "FIRE_ROOT            : $FIRE_ROOT"
echo "SLURM_NTASKS         : ${SLURM_NTASKS:-unset}"
echo "============================================================"

echo "[ENV] Activating conda environment"
if [ -f "$CONDA_INIT" ]; then
    # shellcheck disable=SC1090
    source "$CONDA_INIT"
    conda activate "$CONDA_ENV"
else
    echo "ERROR: CONDA_INIT not found: $CONDA_INIT"
    exit 1
fi

echo "[ENV] which python: $(which python || true)"
echo "[ENV] python version:"
"$PYTHON_EXE" --version

export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

echo "[CHECK] Verifying source paths"
for p in "$BASE_VALIDATION" "$FIRE_ROOT_LOGIN_NODE"; do
    if [ ! -e "$p" ]; then
        echo "ERROR: Required source path does not exist: $p"
        exit 1
    fi
    echo "OK: $p"
done

echo "[STEP A] Preparing compute-node scratch root"
mkdir -p "$FIRE_ROOT"
echo "Scratch root created: $FIRE_ROOT"
ls -ld "$FIRE_ROOT"

echo "[STEP B] Copying pipeline tree to compute-node scratch (excluding Data/)"
rsync -av --delete \
    --exclude 'Data/' \
    "$BASE_VALIDATION"/ \
    "$FIRE_ROOT"/
echo "[STEP B DONE]"

echo "[STEP C] Copying top-level Data/*.py helper scripts"
find "$BASE_VALIDATION/Data" -maxdepth 1 -type f -name '*.py' -print
find "$BASE_VALIDATION/Data" -maxdepth 1 -type f -name '*.py' -exec cp -v {} "$FIRE_ROOT"/ \;
echo "[STEP C DONE]"

echo "[STEP D] Copying prepared case folders from login/shared space"
rsync -av \
    "$FIRE_ROOT_LOGIN_NODE"/ \
    "$FIRE_ROOT"/
echo "[STEP D DONE]"

echo "[STEP E] Listing scratch root contents after copy"
ls -la "$FIRE_ROOT" | sed -n '1,120p'

cd "$FIRE_ROOT"

CASE_LIST="$FIRE_ROOT/case_list.txt"

echo "[STEP F] Building case list from scratch-side case folders"
find "$FIRE_ROOT" -mindepth 1 -maxdepth 1 -type d -printf "%f\n" \
    | grep -E '^[0-9]+$' \
    | sort -n \
    | awk -v root="$FIRE_ROOT" '{print root "/" $0}' > "$CASE_LIST"

if [ ! -s "$CASE_LIST" ]; then
    echo "ERROR: No case folders found on compute node under $FIRE_ROOT"
    exit 1
fi

NCASES="$(wc -l < "$CASE_LIST" | tr -d ' ')"
echo "Found $NCASES case folders on compute node"
echo "Case list preview:"
sed -n '1,40p' "$CASE_LIST"

echo "[STEP G] Verifying required script exists on compute scratch"
if [ ! -f "$FIRE_ROOT/runPipelineParallel.py" ]; then
    echo "ERROR: Missing $FIRE_ROOT/runPipelineParallel.py"
    exit 1
fi
ls -l "$FIRE_ROOT/runPipelineParallel.py"

run_case() {
    local case_dir="$1"
    local case_name
    case_name="$(basename "$case_dir")"
    local case_log="$case_dir/slurm_case_runner.log"

    echo "------------------------------------------------------------"
    echo "[CASE START] $case_name :: $(date) :: $(hostname)"
    echo "case_dir=$case_dir"
    echo "case_log=$case_log"

    if [ ! -d "$case_dir" ]; then
        echo "[CASE ERROR] Missing case dir: $case_dir"
        return 1
    fi

    {
        echo "[CASE INFO] START $(date)"
        echo "[CASE INFO] HOST $(hostname)"
        echo "[CASE INFO] Running: $PYTHON_EXE $FIRE_ROOT/runPipelineParallel.py $case_dir"
        "$PYTHON_EXE" "$FIRE_ROOT/runPipelineParallel.py" "$case_dir"
        rc=$?
        echo "[CASE INFO] END $(date)"
        echo "[CASE INFO] RETURN CODE $rc"
        exit $rc
    } > "$case_log" 2>&1

    local rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "[CASE FAIL] $case_name rc=$rc"
        echo "[CASE FAIL] Last 40 lines of $case_log"
        tail -n 40 "$case_log" || true
        return "$rc"
    fi

    echo "[CASE DONE] $case_name :: rc=0"
    return 0
}

export -f run_case
export FIRE_ROOT PYTHON_EXE

echo "[STEP H] Launching up to 96 parallel case workers"
cat "$CASE_LIST" | xargs -I {} -P 96 bash -c 'run_case "$@"' _ {}

echo "[STEP H DONE] All xargs workers completed"

echo "[STEP I] Post-run summary"
find "$FIRE_ROOT" -mindepth 1 -maxdepth 1 -type d | sort | sed -n '1,40p'

echo "============================================================"
echo "SLURM JOB END"
echo "END TIME             : $(date)"
echo "============================================================"
SLURM

sed -i \
    -e "s|__WALLTIME__|$WALLTIME|g" \
    -e "s|__PARTITION__|$PARTITION|g" \
    -e "s|__SBATCH_MEM__|$SBATCH_MEM|g" \
    -e "s|__LOG_DIR__|$LOG_DIR|g" \
    -e "s|__CALL_DIR__|$CALL_DIR|g" \
    -e "s|__PYTHON_EXE__|$PYTHON_EXE|g" \
    -e "s|__CONDA_INIT__|$CONDA_INIT|g" \
    -e "s|__CONDA_ENV__|$CONDA_ENV|g" \
	-e "s|__BASE_VALIDATION__|$BASE_VALIDATION|g" \
	-e "s|__FIRE_ROOT_LOGIN_NODE__|$FIRE_ROOT_LOGIN_NODE|g" \
	-e "s|__FIRE_ROOT__|$FIRE_ROOT|g" \
    "$SLURM_SCRIPT"

chmod +x "$SLURM_SCRIPT"

echo
echo "[3/4] Submitting Slurm job and waiting for completion"
JOB_ID="$(sbatch --wait --parsable "$SLURM_SCRIPT")"
echo "Slurm job completed: $JOB_ID"

echo
echo "[4/4] Collecting TOA rasters into $TOA_DIR"

FOUND=0
MISSING=0

while IFS= read -r case_dir; do
    case_name="$(basename "$case_dir")"
    case_num=$((10#$case_name))
    printf -v toa_name "TOA_%05d.tif" "$case_num"

    src_toa="$(find "$case_dir/outputs" -maxdepth 1 -type f -name 'time_of_arrival_*.tif' | head -n 1 || true)"

    if [ -n "$src_toa" ] && [ -f "$src_toa" ]; then
        cp -f "$src_toa" "$TOA_DIR/$toa_name"
        echo "Copied: $src_toa -> $TOA_DIR/$toa_name"
        FOUND=$((FOUND + 1))
    else
        echo "WARNING: No TOA file found for case $case_name"
        MISSING=$((MISSING + 1))
    fi
done < "$CASE_LIST"

echo
echo "Done."
echo "TOAs copied: $FOUND"
echo "Cases missing TOA: $MISSING"
echo "Output directory: $TOA_DIR"