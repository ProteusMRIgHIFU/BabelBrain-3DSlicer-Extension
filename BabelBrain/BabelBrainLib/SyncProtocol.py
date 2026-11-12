"""
Communication with BabelBrain through the Brainsight JSON protocol.

BabelBrain (0.8.8 and newer) started with ``-bInUseWithBrainsight`` looks for
``~/.BabelBrainSync/BabelBrainInput.json``. If found, the file is renamed to
``BabelBrainInput-read.json`` and its content (T1w, SimNIBS m2m directory,
trajectories, output directory) pre-fills the BabelBrain input dialog. While
BabelBrain runs, it (re)writes ``~/.BabelBrainSync/BabelBrainOutput.json`` with
the acoustic and thermal maps produced so far and the "modified" trajectories
(trajectory corrected by the mechanical adjustment found in Step 2).

This module mirrors the reader/writer in BabelBrain/BrainsightSync.py and has
no dependency on 3D Slicer (only numpy), so it can be tested on its own.

Pose convention
---------------
A trajectory in 3D Slicer is a linear transform whose matrix "to parent" (RAS,
mm) has the target in the translation column and the trajectory direction
(pointing towards the head, as in SlicerIGT) in the 3rd column. Brainsight
uses the same matrix but with the direction pointing towards the feet; this is
the same conversion done by BabelBrain's ConvMatTransform.itk_to_BSight, which
negates the 3x3 rotation block. A JSON ``position`` is the 3x4 [R|t] matrix in
row-major order (12 values); BabelBrain answers with 16 values (4x4).
"""

import gzip
import json
import os
import re
import struct
import sys
import tempfile
from pathlib import Path

import numpy as np

SCHEMA_VERSION = 1

SYNC_PATH = os.path.join(str(Path.home()), ".BabelBrainSync")
INPUT_JSON = "BabelBrainInput.json"
INPUT_JSON_READ = "BabelBrainInput-read.json"
OUTPUT_JSON = "BabelBrainOutput.json"

LAUNCH_FLAG = "-bInUseWithBrainsight"

# BabelBrain process exit codes (BabelBrain.py ReturnCodes)
RETURN_CODES = {
    0: "success",
    1: "cancelled or incomplete",
    2: "error while generating the domain (Step 1)",
    3: "error in the acoustic simulation (Step 2)",
}

_NIFTI_XFORM_NAMES = {1: "Scanner", 2: "Aligned", 3: "MNI-152", 4: "Talairach", 5: "Other-Template"}


class SyncProtocolError(Exception):
    """Invalid input/output for the BabelBrain JSON protocol."""


###############################################################################
# Pose conversion
###############################################################################

def SlicerMatrixToPosition(matrix_ras):
    """4x4 Slicer (RAS, to parent) trajectory matrix -> 12 values for the JSON input."""
    m = np.array(matrix_ras, dtype=float).reshape(4, 4).copy()
    m[:3, :3] = -m[:3, :3]
    return [float(v) for v in m[:3, :].ravel()]


def PositionToSlicerMatrix(position):
    """12 (3x4) or 16 (4x4) row-major values from BabelBrain -> 4x4 Slicer matrix."""
    arr = np.asarray(position, dtype=float).ravel()
    if arr.size not in (12, 16):
        raise SyncProtocolError("position must have 12 or 16 values, it has %i" % arr.size)
    m = np.eye(4)
    m[:3, :] = arr[:12].reshape(3, 4)
    m[:3, :3] = -m[:3, :3]
    return m


###############################################################################
# Target names
###############################################################################

# BabelBrain rejects tab, new line, / and \ ; names are also used for file
# names (joined with '+'), so we keep a conservative set of characters.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def MakeTargetNames(names):
    """Sanitize trajectory names and make them unique (order is preserved)."""
    result = []
    for name in names:
        base = _UNSAFE_NAME_CHARS.sub("_", name.strip()).strip("_.") or "Trajectory"
        candidate, n = base, 2
        while candidate in result:
            candidate = "%s_%i" % (base, n)
            n += 1
        result.append(candidate)
    return result


###############################################################################
# NIfTI header (NIfTI-1, plain or gzipped) without extra dependencies
###############################################################################

def _QuaternionAffine(b, c, d, qx, qy, qz, pixdim):
    a = 1.0 - (b * b + c * c + d * d)
    a = np.sqrt(a) if a > 1e-7 else 0.0
    R = np.array([[a * a + b * b - c * c - d * d, 2 * (b * c - a * d), 2 * (b * d + a * c)],
                  [2 * (b * c + a * d), a * a + c * c - b * b - d * d, 2 * (c * d - a * b)],
                  [2 * (b * d - a * c), 2 * (c * d + a * b), a * a + d * d - c * c - b * b]])
    qfac = -1.0 if pixdim[0] < 0 else 1.0
    affine = np.eye(4)
    affine[:3, :3] = R @ np.diag([pixdim[1], pixdim[2], pixdim[3] * qfac])
    affine[:3, 3] = [qx, qy, qz]
    return affine


def ReadNiftiHeader(fname):
    """
    Return a dict with qform_code, sform_code and the affine BabelBrain (nibabel)
    uses: sform when sform_code > 0, otherwise qform. Only NIfTI-1 is supported.
    """
    opener = gzip.open if fname.lower().endswith(".gz") else open
    with opener(fname, "rb") as f:
        hdr = f.read(348)
    if len(hdr) < 348:
        raise SyncProtocolError("%s is not a NIfTI file (header too short)" % fname)
    for endian in ("<", ">"):
        if struct.unpack(endian + "i", hdr[:4])[0] == 348:
            break
    else:
        raise SyncProtocolError("%s is not a NIfTI-1 file" % fname)
    if hdr[344:347] not in (b"n+1", b"ni1"):
        raise SyncProtocolError("%s is not a NIfTI-1 file (magic)" % fname)
    pixdim = struct.unpack(endian + "8f", hdr[76:108])
    qform_code, sform_code = struct.unpack(endian + "2h", hdr[252:256])
    b, c, d, qx, qy, qz = struct.unpack(endian + "6f", hdr[256:280])
    srow = np.array(struct.unpack(endian + "12f", hdr[280:328]), dtype=float).reshape(3, 4)
    sform = np.eye(4)
    sform[:3, :] = srow
    qform = _QuaternionAffine(b, c, d, qx, qy, qz, pixdim)
    if sform_code > 0:
        affine = sform
    elif qform_code > 0:
        affine = qform
    else:
        affine = None
    return {"qform_code": int(qform_code), "sform_code": int(sform_code),
            "affine": affine, "sform": sform, "qform": qform}


def CoordinateSystemFromHeader(header):
    """Brainsight-style coordinate system string accepted by BabelBrain."""
    if header["sform_code"] in _NIFTI_XFORM_NAMES:
        return "NIfTI:S:" + _NIFTI_XFORM_NAMES[header["sform_code"]]
    if header["qform_code"] in _NIFTI_XFORM_NAMES:
        return "NIfTI:Q:" + _NIFTI_XFORM_NAMES[header["qform_code"]]
    raise SyncProtocolError("The T1w NIfTI file has neither a valid sform nor qform code")


###############################################################################
# Input JSON
###############################################################################

def BuildInput(anatomical_path, segmentation_path, coordinate_system, targets,
               output_path, created_by):
    """
    targets: list of dicts with keys name, uuid, matrix (4x4 Slicer RAS matrix).
    """
    if len(targets) == 0:
        raise SyncProtocolError("At least one trajectory is required")
    names = [t["name"] for t in targets]
    if len(set(names)) != len(names):
        raise SyncProtocolError("Trajectory names must be unique")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "createdBy": created_by,
        "anatomicalPath": os.path.abspath(anatomical_path),
        "segmentationPath": os.path.abspath(segmentation_path),
        "coordinateSystem": coordinate_system,
        "outputPath": os.path.abspath(output_path),
        "targets": [{"name": t["name"],
                     "uuid": t["uuid"],
                     "position": SlicerMatrixToPosition(t["matrix"])} for t in targets],
    }


def WriteJSONAtomic(fname, data):
    """Write via a temporary file + rename, so BabelBrain never reads a partial file."""
    dirname = os.path.dirname(fname)
    os.makedirs(dirname, exist_ok=True)
    fd, tmpname = tempfile.mkstemp(dir=dirname, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmpname, 0o644)
        os.replace(tmpname, fname)
    except BaseException:
        if os.path.isfile(tmpname):
            os.remove(tmpname)
        raise


def PrepareSyncDirectory(sync_path=SYNC_PATH):
    """
    Remove an output left by a previous session (so it is never mistaken for
    the result of the new one). Returns True if an unconsumed input file from a
    previous launch was found (it will be overwritten).
    """
    os.makedirs(sync_path, exist_ok=True)
    out = os.path.join(sync_path, OUTPUT_JSON)
    if os.path.isfile(out):
        os.remove(out)
    return os.path.isfile(os.path.join(sync_path, INPUT_JSON))


###############################################################################
# Output JSON
###############################################################################

def ReadOutput(fname):
    """
    Parse BabelBrainOutput.json. Returns a dict:
        acoustic / thermal: list of {name, uuid, image, matrix (4x4 Slicer)}
        acousticCombined / thermalCombined: list of {uuids, image}
    """
    with open(fname, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise SyncProtocolError("%s is not valid JSON: %s" % (fname, e))
    if not isinstance(data, dict):
        raise SyncProtocolError("BabelBrain output must be a JSON object")
    version = data.get("schemaVersion")
    if version != SCHEMA_VERSION:
        raise SyncProtocolError("Unsupported BabelBrain output schema version %r (expected %i)"
                                % (version, SCHEMA_VERSION))

    def per_target(key):
        res = []
        for entry in data.get(key, []) or []:
            res.append({"name": entry.get("targetName", ""),
                        "uuid": entry.get("targetUUID", ""),
                        "image": entry.get("imagePath", ""),
                        "matrix": PositionToSlicerMatrix(entry["modifiedPosition"])
                        if "modifiedPosition" in entry else None})
        return res

    def combined(key):
        return [{"uuids": list(e.get("targetUUIDList", [])), "image": e.get("imagePath", "")}
                for e in (data.get(key, []) or [])]

    return {"createdBy": data.get("createdBy", ""),
            "coordinateSystem": data.get("coordinateSystem", ""),
            "acoustic": per_target("acousticResults"),
            "thermal": per_target("thermalResults"),
            "acousticCombined": combined("acousticCombinedResults"),
            "thermalCombined": combined("thermalCombinedResults")}


###############################################################################
# BabelBrain executable
###############################################################################

def DefaultExecutableCandidates(platform=None, environ=None):
    """Paths where the official BabelBrain installers place the executable."""
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    home = str(Path.home())
    if platform == "darwin":
        return ["/Applications/BabelBrain.app/Contents/MacOS/BabelBrain",
                os.path.join(home, "Applications", "BabelBrain.app", "Contents", "MacOS", "BabelBrain")]
    if platform.startswith("win"):
        res = []
        # Per-user Inno Setup installer (BabelBrain 0.8.8+): {autopf} = %LOCALAPPDATA%\Programs
        if environ.get("LOCALAPPDATA"):
            res.append(os.path.join(environ["LOCALAPPDATA"], "Programs", "BabelBrain", "BabelBrain.exe"))
        # Machine-wide installs (older installers)
        for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            if environ.get(var):
                p = os.path.join(environ[var], "BabelBrain", "BabelBrain.exe")
                if p not in res:
                    res.append(p)
        return res
    # Linux: no official frozen release yet; the path must be set by the user
    return []


def ResolveExecutable(path):
    """Accept a macOS .app bundle as well as the executable inside it."""
    if not path:
        return ""
    path = os.path.expanduser(path.strip())
    if path.endswith(".app") and os.path.isdir(path):
        inner = os.path.join(path, "Contents", "MacOS", os.path.splitext(os.path.basename(path))[0])
        if os.path.isfile(inner):
            return inner
    return path


def FindExecutable(user_path=""):
    """User-selected path if set (even if invalid, so errors are visible), else first default found."""
    if user_path:
        return ResolveExecutable(user_path)
    for p in DefaultExecutableCandidates():
        if os.path.isfile(p):
            return p
    return ""


def IsExecutable(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)
