Updated FirePairs pipeline for local setup + HPC per-case execution

1) Edit pipelineConfig.py
   - Set BASE_VALIDATION / FIRE_ROOT for your local machine when running setupPipeline.py.
   - On HPC, point FIRE_ROOT to the scratch location that contains the copied case folders.

2) Local preparation
   python setupPipeline.py

   This now performs:
   - processScarsAndPoints.py
   - separateScarsAndPointsToCases.py
   - getSatelliteEndTimes.py
   - eraseInvalidCases.py
   - writes case_metadata.json into each valid case folder

3) Copy to HPC scratch
   Copy the valid numbered case folders plus the updated scripts.

4) HPC execution options
   Single case:
     python runPipelineParallel.py /path/to/00001

   Whole root sequentially (useful for testing):
     python runPipelineParallel.py --case-root /scratch/.../FirePairs

   Recommended for HPC arrays:
     each array task should point at exactly one case directory and run:
     python runPipelineParallel.py /scratch/.../FirePairs/00001

5) Per-case HPC steps now run independently from case_metadata.json
   - getLandfireProductsForFireSim.py
   - splitLandfireTifBands.py
   - makePhiAndAdjFiles.py
   - downloadWeatherData.py
   - downloadAndRunWindninja.py
   - applyNelsonModel.py
   - getBarrierFile.py
   - createElmfireInputFiles.py
