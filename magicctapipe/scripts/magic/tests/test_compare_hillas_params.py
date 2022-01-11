from compare_hillas_params import compare_hillas_parameters
from pathlib import Path

config = Path("magicctapipe/scripts/magic/tests/compare_hillas_params.yaml").absolute()

def test_compare_hillas_params():
	list1 = compare_hillas_parameters(config_file = config, hillas_key="dl1/hillas_params", subrun=5086952, data_or_mc="Data", 
		test_or_train="Test", plot_image=True)
	assert list1 == [False]*len(list1)
	