import sys
sys.path.insert(0, '.')
from fizz import label
assert label(24) == 'FizzBuzz'
assert label(4) == 'Fizz'
assert label(6) == 'Buzz'
assert label(1) == '1'
print('ok')
