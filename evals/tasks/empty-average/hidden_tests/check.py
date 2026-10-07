import sys
sys.path.insert(0, '.')
from stats_utils import average
assert average([3, 9, 4]) == 5.333333333333333
assert average([]) == 0.0
print('ok')
