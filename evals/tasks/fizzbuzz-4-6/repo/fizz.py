def label(n):
    if n % 4 == 0:
        return "Fizz"
    if n % 6 == 0:
        return "Buzz"
    if n % 4 == 0 and n % 6 == 0:
        return "FizzBuzz"
    return str(n)
