cmake_minimum_required(VERSION 3.16.3...3.28.0 FATAL_ERROR)

project(BabelBrain)

#-----------------------------------------------------------------------------
# Extension meta-information
set(EXTENSION_HOMEPAGE "https://github.com/ProteusMRIgHIFU/BabelBrain-3DSlicer-Extension")
set(EXTENSION_CATEGORY "Focused Ultrasound")
set(EXTENSION_CONTRIBUTORS "Samuel Pichardo (University of Calgary)")
set(EXTENSION_DESCRIPTION "Run BabelBrain transcranial focused ultrasound simulations for trajectories planned in 3D Slicer and load the results back.")
set(EXTENSION_ICONURL "https://raw.githubusercontent.com/ProteusMRIgHIFU/BabelBrain-3DSlicer-Extension/main/BabelBrain.png")
set(EXTENSION_SCREENSHOTURLS "")
set(EXTENSION_DEPENDS "NA")

#-----------------------------------------------------------------------------
# Extension dependencies
find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

#-----------------------------------------------------------------------------
# Extension modules
add_subdirectory(BabelBrain)
## NEXT_MODULE

#-----------------------------------------------------------------------------
include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})
