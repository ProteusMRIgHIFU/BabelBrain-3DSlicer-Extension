import datetime
import logging
import os
import subprocess
import tempfile
import uuid

import numpy as np
import qt
import ctk
import vtk

import slicer
from slicer.i18n import tr as _
from slicer.ScriptedLoadableModule import (ScriptedLoadableModule, ScriptedLoadableModuleLogic,
                                           ScriptedLoadableModuleTest, ScriptedLoadableModuleWidget)
from slicer.util import VTKObservationMixin

from BabelBrainLib import SyncProtocol as SP

EXTENSION_VERSION = "0.1.0"

# Node attributes used to link Slicer nodes to BabelBrain sessions
ATTR_TARGET_UUID = "BabelBrain.TargetUUID"    # on trajectories sent to BabelBrain
ATTR_MOD_OF = "BabelBrain.ModifiedFrom"       # on "_mod" trajectories: ID of the original
ATTR_RESULT_FILE = "BabelBrain.ResultFile"    # on loaded result volumes
ATTR_RESULT_LABEL = "BabelBrain.ResultLabel"  # short "<targetName>_<map>" label of a result volume

SETTINGS_EXECUTABLE = "BabelBrain/ExecutablePath"
SETTINGS_LAST_M2M = "BabelBrain/LastM2MPath"
SETTINGS_LAST_OUTPUT = "BabelBrain/LastOutputPath"


#
# BabelBrain
#


class BabelBrain(ScriptedLoadableModule):
    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("BabelBrain")
        self.parent.categories = [_("Focused Ultrasound")]
        self.parent.dependencies = []
        self.parent.contributors = ["Samuel Pichardo (University of Calgary)"]
        self.parent.helpText = _("""
Run <a href="https://github.com/ProteusMRIgHIFU/BabelBrain">BabelBrain</a> transcranial ultrasound
simulations for one or more trajectories (linear transforms) planned in 3D Slicer.<br>
BabelBrain is started in the same mode used by Brainsight; the acoustic and thermal maps and the
corrected ("_mod") trajectories are loaded back into the scene when BabelBrain is closed.<br>
See the <a href="https://github.com/ProteusMRIgHIFU/BabelBrain-3DSlicer-Extension">extension documentation</a>.
""")
        self.parent.acknowledgementText = _("""
BabelBrain is developed at the University of Calgary (Proteus MRIgHIFU lab).
""")


#
# BabelBrainWidget
#


class BabelBrainWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    def __init__(self, parent=None) -> None:
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)
        self.logic = None
        self._parameterNode = None
        self._updatingGUI = False
        self._pollTimer = None

    # ------------------------------------------------------------------ setup
    def setup(self) -> None:
        ScriptedLoadableModuleWidget.setup(self)
        self.logic = BabelBrainLogic()

        # ---- Inputs
        inputs = ctk.ctkCollapsibleButton()
        inputs.text = _("Inputs")
        self.layout.addWidget(inputs)
        form = qt.QFormLayout(inputs)

        self.trajectorySelector = slicer.qMRMLCheckableNodeComboBox()
        self.trajectorySelector.nodeTypes = ["vtkMRMLTransformNode"]
        self.trajectorySelector.addEnabled = False
        self.trajectorySelector.removeEnabled = False
        self.trajectorySelector.renameEnabled = False
        self.trajectorySelector.setMRMLScene(slicer.mrmlScene)
        self.trajectorySelector.setToolTip(_("Trajectories (linear transforms) to simulate. Check one or more."))
        form.addRow(_("Trajectories:"), self.trajectorySelector)

        self.t1Selector = slicer.qMRMLNodeComboBox()
        self.t1Selector.nodeTypes = ["vtkMRMLScalarVolumeNode"]
        self.t1Selector.showChildNodeTypes = False  # no label maps
        self.t1Selector.noneEnabled = True
        self.t1Selector.addEnabled = False
        self.t1Selector.removeEnabled = False
        self.t1Selector.setMRMLScene(slicer.mrmlScene)
        self.t1Selector.setToolTip(_("T1w volume used to plan the trajectories (same as used by SimNIBS)."))
        form.addRow(_("T1w volume:"), self.t1Selector)

        self.m2mPathEdit = ctk.ctkPathLineEdit()
        self.m2mPathEdit.filters = ctk.ctkPathLineEdit.Dirs
        self.m2mPathEdit.settingKey = "BabelBrain/M2MPathHistory"
        self.m2mPathEdit.setToolTip(_("SimNIBS m2m_<subject> directory of this subject."))
        form.addRow(_("SimNIBS m2m directory:"), self.m2mPathEdit)

        self.outputPathEdit = ctk.ctkPathLineEdit()
        self.outputPathEdit.filters = ctk.ctkPathLineEdit.Dirs
        self.outputPathEdit.settingKey = "BabelBrain/OutputPathHistory"
        self.outputPathEdit.setToolTip(_("Directory where BabelBrain writes its results."))
        form.addRow(_("Output directory:"), self.outputPathEdit)

        # ---- Advanced
        advanced = ctk.ctkCollapsibleButton()
        advanced.text = _("Advanced")
        advanced.collapsed = True
        self.layout.addWidget(advanced)
        aform = qt.QFormLayout(advanced)

        self.executableEdit = ctk.ctkPathLineEdit()
        self.executableEdit.filters = ctk.ctkPathLineEdit.Files | ctk.ctkPathLineEdit.Executable
        self.executableEdit.setToolTip(_(
            "Leave empty to use the default installation "
            "(macOS: /Applications/BabelBrain.app; Windows: %LOCALAPPDATA%\\Programs\\BabelBrain or "
            "Program Files\\BabelBrain). On macOS the .app bundle can be selected."))
        aform.addRow(_("BabelBrain executable:"), self.executableEdit)
        self.executableStatus = qt.QLabel()
        self.executableStatus.wordWrap = True
        aform.addRow("", self.executableStatus)

        self.autoLoadCheck = qt.QCheckBox(_("Load results when BabelBrain closes"))
        self.autoLoadCheck.checked = True
        aform.addRow(self.autoLoadCheck)

        # ---- Actions
        self.runButton = qt.QPushButton(_("Run BabelBrain"))
        self.runButton.enabled = False
        self.runButton.setMinimumHeight(32)
        self.layout.addWidget(self.runButton)

        self.loadButton = qt.QPushButton(_("Load current results"))
        self.loadButton.setToolTip(_("Load the results BabelBrain has reported so far "
                                     "(also works while BabelBrain is still open)."))
        self.layout.addWidget(self.loadButton)

        self.statusLabel = qt.QLabel()
        self.statusLabel.wordWrap = True
        self.statusLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        self.layout.addWidget(self.statusLabel)

        # ---- Results
        results = ctk.ctkCollapsibleButton()
        results.text = _("Results")
        self.layout.addWidget(results)
        rform = qt.QFormLayout(results)

        # Plain combo box (not a node selector) so that results are listed with a short
        # "<targetName>_<map>" label instead of the long file-based node name.
        # Item data: volume node ID.
        self.resultSelector = qt.QComboBox()
        rform.addRow(_("Result map:"), self.resultSelector)
        self._updatingResultList = False
        self._resultListRefreshPending = False

        self.opacitySlider = ctk.ctkSliderWidget()
        self.opacitySlider.minimum = 0.0
        self.opacitySlider.maximum = 1.0
        self.opacitySlider.singleStep = 0.05
        self.opacitySlider.decimals = 2
        self.opacitySlider.value = 0.5
        self.opacitySlider.setToolTip(_("Opacity of the result map over the T1w volume."))
        rform.addRow(_("Opacity:"), self.opacitySlider)

        self.thresholdSlider = ctk.ctkRangeWidget()
        self.thresholdSlider.setToolTip(_("Values of the selected result map below the low limit or above "
                                          "the high limit are not displayed (transparent)."))
        self.thresholdSlider.enabled = False
        rform.addRow(_("Threshold:"), self.thresholdSlider)
        self._updatingThreshold = False

        self.showOverlayButton = qt.QPushButton(_("Show over T1w"))
        self.showOverlayButton.setToolTip(_("Show the selected result map over the T1w volume again "
                                            "(e.g. after selecting another volume in the Data module)."))
        rform.addRow(self.showOverlayButton)

        self.layout.addStretch(1)

        # Long node names (BabelBrain result files) and paths must not widen the panel:
        # combo boxes size to a few characters and labels can shrink (their text wraps).
        for combo in (self.trajectorySelector, self.t1Selector, self.resultSelector):
            combo.sizeAdjustPolicy = qt.QComboBox.AdjustToMinimumContentsLengthWithIcon
            combo.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        for pathEdit in (self.m2mPathEdit, self.outputPathEdit, self.executableEdit):
            pathEdit.sizeAdjustPolicy = ctk.ctkPathLineEdit.AdjustToMinimumContentsLength
            pathEdit.minimumContentsLength = 10
            pathEdit.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        for label in (self.statusLabel, self.executableStatus):
            label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)

        # ---- Connections
        self.trajectorySelector.connect("checkedNodesChanged()", self.updateParameterNodeFromGUI)
        self.t1Selector.connect("currentNodeChanged(vtkMRMLNode*)", self.updateParameterNodeFromGUI)
        self.m2mPathEdit.connect("currentPathChanged(QString)", self.updateParameterNodeFromGUI)
        self.outputPathEdit.connect("currentPathChanged(QString)", self.updateParameterNodeFromGUI)
        self.executableEdit.connect("currentPathChanged(QString)", self.onExecutableChanged)
        self.runButton.connect("clicked(bool)", self.onRunButton)
        self.loadButton.connect("clicked(bool)", self.onLoadButton)
        self.resultSelector.connect("currentIndexChanged(int)", self.onResultSelected)
        self.showOverlayButton.connect("clicked(bool)", self.onShowOverlayButton)
        self.opacitySlider.connect("valueChanged(double)", self.onOpacityChanged)
        self.thresholdSlider.connect("valuesChanged(double,double)", self.onThresholdChanged)

        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)
        for event in (slicer.mrmlScene.NodeAddedEvent, slicer.mrmlScene.NodeRemovedEvent,
                      slicer.mrmlScene.EndImportEvent, slicer.mrmlScene.EndCloseEvent):
            self.addObserver(slicer.mrmlScene, event, self.scheduleResultListRefresh)

        settings = qt.QSettings()
        self.executableEdit.currentPath = settings.value(SETTINGS_EXECUTABLE, "")
        self.onExecutableChanged()

        self._pollTimer = qt.QTimer()
        self._pollTimer.setInterval(1000)
        self._pollTimer.connect("timeout()", self.onPoll)

        self.initializeParameterNode()
        self.refreshResultList()  # results already in the scene (e.g. a saved scene)

    def cleanup(self) -> None:
        self.removeObservers()
        if self._pollTimer:
            self._pollTimer.stop()

    def enter(self) -> None:
        self.initializeParameterNode()

    def exit(self) -> None:
        pass

    def onSceneStartClose(self, caller, event) -> None:
        self._parameterNode = None

    def onSceneEndClose(self, caller, event) -> None:
        if self.parent.isEntered:
            self.initializeParameterNode()

    # ------------------------------------------------------- parameter node
    def initializeParameterNode(self) -> None:
        node = self.logic.getParameterNode()
        if not node.GetParameter("Initialized"):
            settings = qt.QSettings()
            node.SetParameter("M2MPath", settings.value(SETTINGS_LAST_M2M, ""))
            node.SetParameter("OutputPath", settings.value(SETTINGS_LAST_OUTPUT, ""))
            node.SetParameter("Initialized", "1")
        self._parameterNode = node
        self.updateGUIFromParameterNode()

    def updateGUIFromParameterNode(self) -> None:
        node = self._parameterNode
        if node is None:
            return
        self._updatingGUI = True
        try:
            selected = set(node.GetNodeReferenceID("Trajectory", n)
                           for n in range(node.GetNumberOfNodeReferences("Trajectory")))
            for n in range(self.trajectorySelector.nodeCount()):
                tnode = self.trajectorySelector.nodeFromIndex(n)
                self.trajectorySelector.setCheckState(
                    tnode, qt.Qt.Checked if tnode.GetID() in selected else qt.Qt.Unchecked)
            self.t1Selector.setCurrentNode(node.GetNodeReference("T1w"))
            self.m2mPathEdit.currentPath = node.GetParameter("M2MPath")
            self.outputPathEdit.currentPath = node.GetParameter("OutputPath")
        finally:
            self._updatingGUI = False
        self.updateButtons()

    def updateParameterNodeFromGUI(self, *args) -> None:
        node = self._parameterNode
        if node is None or self._updatingGUI:
            return
        wasModified = node.StartModify()
        node.RemoveNodeReferenceIDs("Trajectory")
        for tnode in self.trajectorySelector.checkedNodes():
            node.AddNodeReferenceID("Trajectory", tnode.GetID())
        node.SetNodeReferenceID("T1w", self.t1Selector.currentNodeID)
        node.SetParameter("M2MPath", self.m2mPathEdit.currentPath.strip())
        node.SetParameter("OutputPath", self.outputPathEdit.currentPath.strip())
        node.EndModify(wasModified)
        self.updateButtons()

    # ---------------------------------------------------------------- state
    def onExecutableChanged(self, *args) -> None:
        userPath = self.executableEdit.currentPath.strip()
        qt.QSettings().setValue(SETTINGS_EXECUTABLE, userPath)
        exe = SP.FindExecutable(userPath)
        if SP.IsExecutable(exe):
            self.executableStatus.text = _("Using: ") + exe
        elif userPath:
            self.executableStatus.text = _("Not an executable file: ") + exe
        elif slicer.app.os == "linux":
            self.executableStatus.text = _("There is no default BabelBrain installation on Linux; "
                                           "select the executable (or a wrapper script) above.")
        else:
            self.executableStatus.text = _("BabelBrain not found in the default locations: ") + \
                ", ".join(SP.DefaultExecutableCandidates())
        self.updateButtons()

    def missingInputs(self):
        missing = []
        checked = self.trajectorySelector.checkedNodes()
        if len(checked) == 0:
            missing.append(_("at least one trajectory"))
        nonLinear = [n.GetName() for n in checked if not n.IsLinear()]
        if nonLinear:
            missing.append(_("linear trajectories (not linear: %s)") % ", ".join(nonLinear))
        if self.t1Selector.currentNode() is None:
            missing.append(_("T1w volume"))
        m2m = self.m2mPathEdit.currentPath.strip()
        if not m2m:
            missing.append(_("SimNIBS m2m directory"))
        elif not os.path.isdir(m2m):
            missing.append(_("an existing SimNIBS m2m directory"))
        if not self.outputPathEdit.currentPath.strip():
            missing.append(_("output directory"))
        if not SP.IsExecutable(SP.FindExecutable(self.executableEdit.currentPath.strip())):
            missing.append(_("BabelBrain executable (see Advanced)"))
        return missing

    def updateButtons(self) -> None:
        if self.logic is None:
            return
        running = self.logic.isRunning()
        missing = self.missingInputs()
        self.runButton.enabled = (not running) and len(missing) == 0
        if running:
            self.runButton.toolTip = _("BabelBrain is running")
        elif missing:
            self.runButton.toolTip = _("Missing: ") + ", ".join(missing)
            if not self.statusLabel.text or self.statusLabel.property("isHint"):
                self.statusLabel.text = _("Required: ") + ", ".join(missing) + "."
                self.statusLabel.setProperty("isHint", True)
        else:
            self.runButton.toolTip = _("Start BabelBrain with the selected inputs")
            if self.statusLabel.property("isHint"):
                self.statusLabel.text = _("Ready.")
        self.loadButton.enabled = self.logic.findOutputFile() is not None

    def setStatus(self, text) -> None:
        self.statusLabel.setProperty("isHint", False)
        self.statusLabel.text = text
        self.statusLabel.toolTip = text
        logging.info("BabelBrain: " + text)

    # -------------------------------------------------------------- actions
    def onRunButton(self) -> None:
        m2m = self.m2mPathEdit.currentPath.strip()
        outdir = self.outputPathEdit.currentPath.strip()
        settings = qt.QSettings()
        settings.setValue(SETTINGS_LAST_M2M, m2m)
        settings.setValue(SETTINGS_LAST_OUTPUT, outdir)
        self.m2mPathEdit.addCurrentPathToHistory()
        self.outputPathEdit.addCurrentPathToHistory()

        exe = SP.FindExecutable(self.executableEdit.currentPath.strip())
        try:
            with slicer.util.tryWithErrorDisplay(_("Failed to start BabelBrain."), waitCursor=True):
                warnings = self.logic.launch(self.trajectorySelector.checkedNodes(),
                                             self.t1Selector.currentNode(), m2m, outdir, [exe])
        except Exception:
            self.updateButtons()
            return
        if warnings:
            slicer.util.warningDisplay("\n\n".join(warnings), windowTitle=_("BabelBrain"))
        self.setStatus(_("BabelBrain is running. Slicer remains usable; results will be loaded "
                         "when BabelBrain is closed.\nLog: ") + self.logic.logFile)
        self._pollTimer.start()
        self.updateButtons()

    def onPoll(self) -> None:
        code = self.logic.poll()
        if code is None:
            self.loadButton.enabled = self.logic.findOutputFile() is not None
            return
        self._pollTimer.stop()
        desc = SP.RETURN_CODES.get(code, _("unknown exit code"))
        msg = _("BabelBrain finished (%s, code %i).") % (desc, code)
        outFile = self.logic.findOutputFile()
        if outFile and self.autoLoadCheck.checked:
            msg += "\n" + self.loadResults(outFile)
        elif outFile is None:
            msg += "\n" + _("BabelBrain did not report any results.")
            if code not in (0, 1):
                slicer.util.warningDisplay(msg + "\n" + _("See log: ") + self.logic.logFile,
                                           windowTitle=_("BabelBrain"))
        self.setStatus(msg)
        self.updateButtons()

    def onLoadButton(self) -> None:
        outFile = self.logic.findOutputFile()
        if outFile is None:
            slicer.util.warningDisplay(_("No BabelBrain results found."))
            return
        self.setStatus(self.loadResults(outFile))

    def loadResults(self, outFile) -> str:
        try:
            with slicer.util.tryWithErrorDisplay(_("Failed to load BabelBrain results."), waitCursor=True):
                summary = self.logic.loadResults(outFile)
        except Exception:
            return _("Failed to load results from ") + outFile
        if self.logic.lastLoadedVolumes:
            # Select the first map and show it over the whole T1w
            first = self.logic.lastLoadedVolumes[0]
            self.refreshResultList()
            self._updatingResultList = True
            try:
                self.setCurrentResultNode(first)
            finally:
                self._updatingResultList = False
            self.updateResultToolTip()
            self.updateThresholdFromNode()
            self.onShowOverlay(fit=True)
        return summary

    # ---------------------------------------------------------- result list
    def scheduleResultListRefresh(self, *args) -> None:
        # Attributes of a new node are set after NodeAddedEvent: refresh once control returns
        # to the event loop (also coalesces the many events of a scene import).
        if not self._resultListRefreshPending:
            self._resultListRefreshPending = True
            qt.QTimer.singleShot(0, self.refreshResultList)

    def refreshResultList(self) -> None:
        self._resultListRefreshPending = False
        currentID = self.resultSelector.currentData
        nodes = [n for n in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")
                 if n.GetAttribute(ATTR_RESULT_FILE)]
        self._updatingResultList = True
        try:
            self.resultSelector.clear()
            for node in nodes:
                self.resultSelector.addItem(node.GetAttribute(ATTR_RESULT_LABEL) or node.GetName(), node.GetID())
                self.resultSelector.setItemData(self.resultSelector.count - 1, node.GetName(), qt.Qt.ToolTipRole)
            index = self.resultSelector.findData(currentID) if currentID else -1
            self.resultSelector.setCurrentIndex(max(index, 0) if nodes else -1)
        finally:
            self._updatingResultList = False
        self.updateResultToolTip()

    def currentResultNode(self):
        nodeID = self.resultSelector.currentData
        return slicer.mrmlScene.GetNodeByID(nodeID) if nodeID else None

    def setCurrentResultNode(self, node) -> None:
        index = self.resultSelector.findData(node.GetID()) if node else -1
        if index >= 0:
            self.resultSelector.setCurrentIndex(index)

    def onResultSelected(self, index) -> None:
        self.updateResultToolTip()
        self.updateThresholdFromNode()
        if not self._updatingResultList:
            self.onShowOverlay(fit=False)  # only swap the overlay, keep the current views

    def updateResultToolTip(self) -> None:
        node = self.currentResultNode()
        self.resultSelector.toolTip = node.GetName() if node else _(
            "BabelBrain result maps loaded in the scene. The selected map is shown over the T1w volume.")

    def onShowOverlayButton(self, *args) -> None:
        self.onShowOverlay(fit=True)

    def onShowOverlay(self, fit=False) -> None:
        node = self.currentResultNode()
        if node is None:
            return
        self.logic.showOverlay(node, self.t1Selector.currentNode(), self.opacitySlider.value, fit=fit)

    def updateThresholdFromNode(self) -> None:
        """Range of the threshold slider = range of the selected map; values = its current threshold."""
        node = self.currentResultNode()
        display = node.GetDisplayNode() if node else None
        self.thresholdSlider.enabled = display is not None and node.GetImageData() is not None
        if not self.thresholdSlider.enabled:
            return
        low, high = node.GetImageData().GetScalarRange()
        span = high - low
        decimals = 0 if span >= 1000 else 1 if span >= 10 else 3 if span >= 0.01 else 6
        self._updatingThreshold = True
        try:
            self.thresholdSlider.decimals = decimals
            self.thresholdSlider.singleStep = span / 1000.0 if span > 0 else 1.0
            self.thresholdSlider.setRange(low, high)
            if display.GetApplyThreshold():
                self.thresholdSlider.setValues(max(low, display.GetLowerThreshold()),
                                               min(high, display.GetUpperThreshold()))
            else:
                self.thresholdSlider.setValues(low, high)
        finally:
            self._updatingThreshold = False

    def onThresholdChanged(self, low, high) -> None:
        node = self.currentResultNode()
        if node is None or self._updatingThreshold:
            return
        self.logic.setThreshold(node, low, high)

    def onOpacityChanged(self, value) -> None:
        if self.currentResultNode() is not None:
            slicer.util.setSliceViewerLayers(foregroundOpacity=value)


#
# BabelBrainLogic
#


class BabelBrainLogic(ScriptedLoadableModuleLogic):
    def __init__(self, syncPath=SP.SYNC_PATH) -> None:
        ScriptedLoadableModuleLogic.__init__(self)
        self.syncPath = syncPath
        self.process = None
        self.logFile = ""
        self._logHandle = None
        self.outputPath = ""
        self.targetNames = []
        self.lastLoadedVolumes = []

    # ---------------------------------------------------------------- input
    def prepareT1w(self, volumeNode, outputPath):
        """
        Returns (path to a NIfTI-1 file of the volume, coordinate system, warnings).
        The file loaded in Slicer is used when possible; otherwise the volume is
        exported to the output directory.
        """
        warnings = []
        if volumeNode.GetParentTransformNode() is not None:
            raise ValueError(_("The T1w volume '%s' is under a transform. Harden or remove the "
                               "transform first, so that the trajectories and the T1w file share "
                               "the same coordinates.") % volumeNode.GetName())
        storage = volumeNode.GetStorageNode()
        fname = storage.GetFileName() if storage else None
        header = None
        if fname and os.path.isfile(fname) and not volumeNode.GetModifiedSinceRead() \
                and fname.lower().endswith((".nii", ".nii.gz")):
            try:
                header = SP.ReadNiftiHeader(fname)
            except SP.SyncProtocolError as e:
                logging.info("BabelBrain: %s; the volume will be exported" % e)
        if header is None or header["affine"] is None:
            name = SP.MakeTargetNames([volumeNode.GetName()])[0]
            fname = os.path.join(outputPath, name + "_T1w.nii.gz")
            slicer.util.exportNode(volumeNode, fname)
            header = SP.ReadNiftiHeader(fname)
            warnings.append(_("The T1w volume was not loaded from a NIfTI file (or was modified); "
                              "it was saved as\n%s.\nMake sure it is the same T1w used by SimNIBS.") % fname)

        # The trajectories are in Slicer world coordinates; BabelBrain reads the
        # T1w with nibabel. Both must agree on the voxel -> RAS mapping.
        ijkToRas = vtk.vtkMatrix4x4()
        volumeNode.GetIJKToRASMatrix(ijkToRas)
        slicerAffine = slicer.util.arrayFromVTKMatrix(ijkToRas)
        if not np.allclose(slicerAffine, header["affine"], atol=1e-3):
            warnings.append(_("The voxel-to-world transform of the T1w volume in Slicer differs from the "
                              "one BabelBrain will use from the NIfTI header (sform/qform mismatch?). "
                              "Trajectories may be misplaced.\nSlicer:\n%s\nNIfTI:\n%s")
                            % (np.array2string(slicerAffine, precision=3),
                               np.array2string(header["affine"], precision=3)))
        return fname, SP.CoordinateSystemFromHeader(header), warnings

    def buildTargets(self, transformNodes):
        names = SP.MakeTargetNames([n.GetName() for n in transformNodes])
        targets = []
        for node, name in zip(transformNodes, names):
            m = vtk.vtkMatrix4x4()
            node.GetMatrixTransformToWorld(m)
            targetUUID = str(uuid.uuid4())
            node.SetAttribute(ATTR_TARGET_UUID, targetUUID)
            targets.append({"name": name, "uuid": targetUUID, "matrix": slicer.util.arrayFromVTKMatrix(m)})
        return targets

    # --------------------------------------------------------------- launch
    def isRunning(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def launch(self, transformNodes, volumeNode, m2mPath, outputPath, command, env=None):
        """
        Write the input JSON and start BabelBrain (non blocking).
        command: list with the executable (and optional extra arguments);
        the -bInUseWithBrainsight flag is appended.
        env: environment for the process; by default the environment Slicer was
        started with (Slicer's own Python/Qt settings would break BabelBrain).
        Returns a list of warnings for the user.
        """
        if self.isRunning():
            raise RuntimeError(_("BabelBrain is already running."))
        if len(transformNodes) == 0:
            raise ValueError(_("Select at least one trajectory."))
        nonLinear = [n.GetName() for n in transformNodes if not n.IsLinear()]
        if nonLinear:
            raise ValueError(_("Trajectories must be linear transforms: ") + ", ".join(nonLinear))
        if not os.path.isdir(m2mPath):
            raise ValueError(_("SimNIBS m2m directory does not exist:\n") + m2mPath)
        os.makedirs(outputPath, exist_ok=True)

        t1Path, coordinateSystem, warnings = self.prepareT1w(volumeNode, outputPath)
        targets = self.buildTargets(transformNodes)
        data = SP.BuildInput(t1Path, m2mPath, coordinateSystem, targets, outputPath,
                             "3D Slicer %s (BabelBrain extension %s)"
                             % (slicer.app.applicationVersion, EXTENSION_VERSION))

        if SP.PrepareSyncDirectory(self.syncPath):
            logging.warning("BabelBrain: replacing an unread %s from a previous launch" % SP.INPUT_JSON)
        SP.WriteJSONAtomic(os.path.join(self.syncPath, SP.INPUT_JSON), data)

        if env is None:
            env = dict(slicer.util.startupEnvironment())
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.logFile = os.path.join(outputPath, "BabelBrain-Slicer-%s.log" % stamp)
        self._logHandle = open(self.logFile, "w")
        args = list(command) + [SP.LAUNCH_FLAG]
        logging.info("BabelBrain: starting " + " ".join(args))
        try:
            self.process = subprocess.Popen(args, env=env, cwd=outputPath,
                                            stdin=subprocess.DEVNULL,
                                            stdout=self._logHandle, stderr=subprocess.STDOUT)
        except Exception:
            self._closeLog()
            raise
        self.outputPath = outputPath
        self.targetNames = [t["name"] for t in targets]
        return warnings

    def poll(self):
        """None while running, else the exit code (once the process is done)."""
        if self.process is None:
            return None
        code = self.process.poll()
        if code is not None:
            self.process = None
            self._closeLog()
            self.archiveOutput()
        return code

    def _closeLog(self):
        if self._logHandle:
            self._logHandle.close()
            self._logHandle = None

    def archiveOutput(self):
        """Keep a copy of the output JSON next to the results (the sync file is reset on every launch)."""
        src = os.path.join(self.syncPath, SP.OUTPUT_JSON)
        if self.outputPath and os.path.isfile(src):
            import shutil
            dst = os.path.join(self.outputPath, "+".join(self.targetNames) + "_" + SP.OUTPUT_JSON)
            shutil.copyfile(src, dst)

    def findOutputFile(self):
        p = os.path.join(self.syncPath, SP.OUTPUT_JSON)
        return p if os.path.isfile(p) else None

    # -------------------------------------------------------------- results
    def _findTrajectory(self, targetUUID, targetName):
        for node in slicer.util.getNodesByClass("vtkMRMLTransformNode"):
            if targetUUID and node.GetAttribute(ATTR_TARGET_UUID) == targetUUID:
                return node
        for node in slicer.util.getNodesByClass("vtkMRMLTransformNode"):
            if node.GetAttribute(ATTR_MOD_OF) is None and \
                    SP.MakeTargetNames([node.GetName()])[0] == targetName:
                return node
        return None

    def _updateModTrajectory(self, entry, folderItem):
        original = self._findTrajectory(entry["uuid"], entry["name"])
        name = (original.GetName() if original else entry["name"]) + "_mod"
        modNode = None
        if original is not None:
            for node in slicer.util.getNodesByClass("vtkMRMLTransformNode"):
                if node.GetAttribute(ATTR_MOD_OF) == original.GetID():
                    modNode = node
                    break
        if modNode is None:
            modNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode", name)
            modNode.SetAttribute(ATTR_MOD_OF, original.GetID() if original else "")
            modNode.CreateDefaultDisplayNodes()
        # BabelBrain reports world coordinates
        modNode.SetAndObserveTransformNodeID(None)
        modNode.SetMatrixTransformToParent(slicer.util.vtkMatrixFromArray(entry["matrix"]))
        shNode = slicer.mrmlScene.GetSubjectHierarchyNode()
        shNode.SetItemParent(shNode.GetItemByDataNode(modNode), folderItem)
        return modNode

    @staticmethod
    def resultLabel(targetName, mapType):
        """Short label of a result map, e.g. "Target1_FullElasticSolution"."""
        return "%s_%s" % (targetName, mapType)

    @staticmethod
    def acousticMapType(path):
        # phased arrays report the refocused solution
        return "FullElasticSolutionRefocus" if "FullElasticSolutionRefocus" in os.path.basename(path) \
            else "FullElasticSolution"

    @staticmethod
    def jetColorNode():
        """Jet color table (not provided by Slicer); created once per scene."""
        node = slicer.util.getFirstNodeByClassByName("vtkMRMLColorTableNode", "Jet")
        if node is None:
            node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLColorTableNode", "Jet")
            node.SetTypeToUser()
            n = 256
            node.SetNumberOfColors(n)
            node.SetNamesInitialised(True)
            for i in range(n):
                x = i / (n - 1.0)
                r, g, b = (float(np.clip(1.5 - abs(4.0 * x - k), 0.0, 1.0)) for k in (3.0, 2.0, 1.0))
                node.SetColor(i, "", r, g, b, 1.0)
        return node

    def _loadVolume(self, path, folderItem, label=""):
        if not path or not os.path.isfile(path):
            logging.warning("BabelBrain: result file not found: %s" % path)
            return None
        for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
            if node.GetAttribute(ATTR_RESULT_FILE) == path:
                slicer.mrmlScene.RemoveNode(node)  # file may have been recomputed
        name = os.path.basename(path)
        for ext in (".nii.gz", ".nii"):
            if name.endswith(ext):
                name = name[: -len(ext)]
        node = slicer.util.loadVolume(path, {"name": name, "show": False})
        node.SetAttribute(ATTR_RESULT_LABEL, label or name)
        node.SetAttribute(ATTR_RESULT_FILE, path)
        display = node.GetDisplayNode()
        if display:
            display.SetAndObserveColorNodeID(self.jetColorNode().GetID())
            # Colormap spans the full range of the map (not Slicer's automatic window/level)
            low, high = node.GetImageData().GetScalarRange()
            display.AutoWindowLevelOff()
            display.SetWindowLevelMinMax(low, high)
        shNode = slicer.mrmlScene.GetSubjectHierarchyNode()
        shNode.SetItemParent(shNode.GetItemByDataNode(node), folderItem)
        return node

    def showOverlay(self, resultVolume, backgroundVolume=None, opacity=0.5, fit=False) -> None:
        """
        Show a result map over the T1w (background kept as is if not given) in all slice views.
        fit: also fit the views to the background. Showing a (cropped) result alone, e.g. from
        the Data module, zooms the views to its small extent; fitting shows the whole T1w again.
        Without fit, the current zoom/pan of the views is kept.
        """
        slicer.util.setSliceViewerLayers(
            background=backgroundVolume if backgroundVolume is not None else "keep-current",
            foreground=resultVolume, foregroundOpacity=opacity, fit=fit)

    @staticmethod
    def setThreshold(resultVolume, low, high) -> None:
        """Values outside [low, high] are transparent in the slice views."""
        display = resultVolume.GetDisplayNode()
        if display:
            display.SetApplyThreshold(True)
            display.SetThreshold(low, high)

    def loadResults(self, outFile, backgroundVolume=None, showFirst=False) -> str:
        """
        Create/update "_mod" trajectories and load all result images. Returns a summary.
        The loaded volumes are kept in self.lastLoadedVolumes; with showFirst, the
        first one is shown over backgroundVolume.
        """
        results = SP.ReadOutput(outFile)
        shNode = slicer.mrmlScene.GetSubjectHierarchyNode()
        folderItem = shNode.CreateFolderItem(
            shNode.GetSceneItemID(),
            "BabelBrain results " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        modNodes = {}
        for entry in results["acoustic"] + results["thermal"]:
            if entry["matrix"] is not None:
                modNodes[entry["uuid"] or entry["name"]] = self._updateModTrajectory(entry, folderItem)

        volumes = []
        for key in ("acoustic", "acousticCombined", "thermal", "thermalCombined"):
            for entry in results[key]:
                # Combined (merged) results have no single target name
                targetName = entry.get("name") or _("Combined")
                if key.startswith("acoustic"):
                    mapType = self.acousticMapType(entry["image"])
                else:
                    mapType = "ThermalField"
                node = self._loadVolume(entry["image"], folderItem, self.resultLabel(targetName, mapType))
                if node is not None:
                    volumes.append(node)
                    # Thermal maps come with pressure/intensity/MI maps
                    if key.startswith("thermal") and "ThermalField" in entry["image"]:
                        for extra in ("PressureField", "IntensityField", "MI"):
                            extraPath = entry["image"].replace("ThermalField", extra)
                            if os.path.isfile(extraPath):
                                n = self._loadVolume(extraPath, folderItem, self.resultLabel(targetName, extra))
                                if n is not None:
                                    volumes.append(n)

        self.lastLoadedVolumes = volumes
        if volumes and showFirst:
            self.showOverlay(volumes[0], backgroundVolume, fit=True)
        if not volumes and not modNodes:
            shNode.RemoveItem(folderItem)
        return _("Loaded %i result image(s) and %i modified trajectory(ies) from %s") \
            % (len(volumes), len(modNodes), outFile)


#
# BabelBrainTest
#


MOCK_BABELBRAIN = r'''
import json, os, shutil, sys
sync = os.environ["BABELBRAIN_SLICER_TEST_SYNC"]
assert "-bInUseWithBrainsight" in sys.argv
src = os.path.join(sync, "BabelBrainInput.json")
dst = os.path.join(sync, "BabelBrainInput-read.json")
os.replace(src, dst)
inp = json.load(open(dst))
out = {"schemaVersion": 1, "createdBy": "MockBabelBrain", "coordinateSystem": inp["coordinateSystem"],
       "acousticResults": [], "acousticCombinedResults": [], "thermalResults": [], "thermalCombinedResults": []}
for t in inp["targets"]:
    img = os.path.join(inp["outputPath"], t["name"] + "_FullElasticSolution_Sub_NORM.nii.gz")
    shutil.copyfile(inp["anatomicalPath"], img)
    p = list(t["position"]) + [0, 0, 0, 1]
    p[3] -= 1.0  # mechanical correction of 1 mm in x (applied to the translation)
    out["acousticResults"].append({"targetName": t["name"], "targetUUID": t["uuid"],
                                   "imagePath": img, "modifiedPosition": p})
if len(inp["targets"]) > 1:
    img = os.path.join(inp["outputPath"], "Merged_NORM.nii.gz")
    shutil.copyfile(inp["anatomicalPath"], img)
    out["acousticCombinedResults"].append({"targetUUIDList": [t["uuid"] for t in inp["targets"]],
                                           "imagePath": img})
json.dump(out, open(os.path.join(sync, "BabelBrainOutput.json"), "w"))
sys.exit(0)
'''


class BabelBrainTest(ScriptedLoadableModuleTest):
    def setUp(self):
        slicer.mrmlScene.Clear()

    def runTest(self):
        self.setUp()
        self.test_PoseConversion()
        self.setUp()
        self.test_NiftiHeader()
        self.setUp()
        self.test_EndToEndWithMockBabelBrain()

    def _makeVolume(self, folder):
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "T1 test")
        slicer.util.updateVolumeFromArray(node, np.random.rand(10, 12, 14).astype(np.float32))
        node.SetSpacing(1.0, 1.1, 1.2)
        node.SetOrigin(-10, 20, 5)
        fname = os.path.join(folder, "T1.nii.gz")
        slicer.util.saveNode(node, fname)
        return node, fname

    def _makeTrajectory(self, name, translation, className="vtkMRMLTransformNode"):
        # The Transforms module of Slicer 5.x creates generic vtkMRMLTransformNode nodes
        node = slicer.mrmlScene.AddNewNodeByClass(className, name)
        angle = np.deg2rad(20)
        m = np.eye(4)
        m[:3, :3] = [[1, 0, 0], [0, np.cos(angle), -np.sin(angle)], [0, np.sin(angle), np.cos(angle)]]
        m[:3, 3] = translation
        node.SetMatrixTransformToParent(slicer.util.vtkMatrixFromArray(m))
        return node, m

    def test_PoseConversion(self):
        self.delayDisplay("Pose conversion")
        m = np.eye(4)
        m[:3, :3] = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
        m[:3, 3] = [1, 2, 3]
        pos = SP.SlicerMatrixToPosition(m)
        self.assertEqual(len(pos), 12)
        # Same convention as BabelBrain ConvMatTransform.itk_to_BSight (rotation negated)
        bsight = np.eye(4)
        bsight[:3, :] = np.array(pos).reshape(3, 4)
        np.testing.assert_allclose(bsight[:3, :3], -m[:3, :3])
        np.testing.assert_allclose(bsight[:3, 3], m[:3, 3])
        np.testing.assert_allclose(SP.PositionToSlicerMatrix(pos), m)
        np.testing.assert_allclose(SP.PositionToSlicerMatrix(list(bsight.ravel())), m)
        self.assertEqual(SP.MakeTargetNames(["a b", "a/b", "a b", ""]), ["a_b", "a_b_2", "a_b_3", "Trajectory"])
        self.delayDisplay("Pose conversion passed")

    def test_NiftiHeader(self):
        self.delayDisplay("NIfTI header")
        with tempfile.TemporaryDirectory() as folder:
            node, fname = self._makeVolume(folder)
            header = SP.ReadNiftiHeader(fname)
            ijkToRas = vtk.vtkMatrix4x4()
            node.GetIJKToRASMatrix(ijkToRas)
            np.testing.assert_allclose(header["affine"], slicer.util.arrayFromVTKMatrix(ijkToRas), atol=1e-4)
            self.assertTrue(SP.CoordinateSystemFromHeader(header).startswith("NIfTI:"))
            logic = BabelBrainLogic()
            node = slicer.util.loadVolume(fname)
            path, cs, warnings = logic.prepareT1w(node, folder)
            self.assertEqual(os.path.abspath(path), os.path.abspath(fname))
            self.assertEqual(warnings, [])
        self.delayDisplay("NIfTI header passed")

    def test_EndToEndWithMockBabelBrain(self):
        self.delayDisplay("End to end with a mock BabelBrain")
        import sys
        import time
        with tempfile.TemporaryDirectory() as folder:
            sync = os.path.join(folder, "sync")
            outdir = os.path.join(folder, "out")
            m2m = os.path.join(folder, "m2m_test")
            os.makedirs(m2m)
            _, fname = self._makeVolume(folder)
            t1 = slicer.util.loadVolume(fname)
            traj1, m1 = self._makeTrajectory("Target 1", [1, 2, 3])
            traj2, m2 = self._makeTrajectory("Target 2", [4, 5, 6], "vtkMRMLLinearTransformNode")
            mock = os.path.join(folder, "mock_babelbrain.py")
            with open(mock, "w") as f:
                f.write(MOCK_BABELBRAIN)

            logic = BabelBrainLogic(syncPath=sync)
            env = dict(os.environ)
            env["BABELBRAIN_SLICER_TEST_SYNC"] = sync
            logic.launch([traj1, traj2], t1, m2m, outdir, [sys.executable, mock], env=env)

            code = None
            for _i in range(300):
                code = logic.poll()
                if code is not None:
                    break
                time.sleep(0.1)
            self.assertEqual(code, 0, open(logic.logFile).read())
            self.assertTrue(os.path.isfile(os.path.join(sync, SP.INPUT_JSON_READ)))
            self.assertTrue(os.path.isfile(os.path.join(outdir, "Target_1+Target_2_" + SP.OUTPUT_JSON)))

            summary = logic.loadResults(logic.findOutputFile(), t1)
            self.assertIn("3 result image", summary)
            for traj, m in ((traj1, m1), (traj2, m2)):
                mods = [n for n in slicer.util.getNodesByClass("vtkMRMLTransformNode")
                        if n.GetAttribute(ATTR_MOD_OF) == traj.GetID()]
                self.assertEqual(len(mods), 1)
                self.assertEqual(mods[0].GetName(), traj.GetName() + "_mod")
                expected = m.copy()
                expected[0, 3] -= 1.0
                mm = vtk.vtkMatrix4x4()
                mods[0].GetMatrixTransformToParent(mm)
                np.testing.assert_allclose(slicer.util.arrayFromVTKMatrix(mm), expected, atol=1e-6)

            labels = sorted(n.GetAttribute(ATTR_RESULT_LABEL) for n in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")
                            if n.GetAttribute(ATTR_RESULT_FILE))
            self.assertEqual(labels, ["Combined_FullElasticSolution", "Target_1_FullElasticSolution",
                                      "Target_2_FullElasticSolution"])

            # Loading again updates the existing "_mod" nodes and replaces the volumes
            logic.loadResults(logic.findOutputFile(), t1)
            self.assertEqual(len([n for n in slicer.util.getNodesByClass("vtkMRMLTransformNode")
                                  if n.GetAttribute(ATTR_MOD_OF)]), 2)
            self.assertEqual(len([n for n in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")
                                  if n.GetAttribute(ATTR_RESULT_FILE)]), 3)
            slicer.mrmlScene.Clear()  # release files before the temporary folder is removed
        self.delayDisplay("End to end passed")
