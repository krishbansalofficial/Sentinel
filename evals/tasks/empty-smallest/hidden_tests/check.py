import sys
sys.path.insert(0, '.')
from stats_utils import smallest
assert smallest([3, 9, 4]) == 3
assert smallest([]) == None
print('ok')
