# The script expects to be run from a directory containing the visualizer executable, a set of input files in a subdirectory called "testInput", and a set of reference files in a subdirectory called "referenceOut".  When running it will fill a directory name "testOutput" then compare corresponding files between testOutput and referenceOut.

ls testInput > listOfTestFiles
sed -e "s/^\(.*\).csv$/python3 swi3s_visualizer.py --batch --config_file \"testInput\/\1.csv\" --output_frame_file \"testOutput\/\1.out\"/g" < listOfTestFiles | sed -e "s/\([()]\)/\1/g" > exec.bash
sed -e "s/^\(.*\).csv$/cmp \"testOutput\/\1.out\" \"referenceOut\/\1.out\"/g" < listOfTestFiles | sed -e "s/\([()]\)/\1/g" > compare.bash
source exec.bash
source compare.bash


# python3 -m pdb swi3s_visualizer.py --batch
