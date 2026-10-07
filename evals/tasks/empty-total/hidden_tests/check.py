import sys
sys.path.insert(0, '.')
from stats_utils import total
assert total([3, 9, 4]) == 16
assert total([]) == 0
print('ok')
