import sys
sys.path.insert(0, '.')
from words import shout
assert shout('hi') == 'HI!'
print('ok')
