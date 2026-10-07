import sys
sys.path.insert(0, '.')
from stats_utils import largest
assert largest([3, 9, 4]) == 9
assert largest([]) == None
print('ok')
