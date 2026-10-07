import sys
sys.path.insert(0, '.')
from stats_utils import first
assert first([3, 9, 4]) == 3
assert first([]) == None
print('ok')
