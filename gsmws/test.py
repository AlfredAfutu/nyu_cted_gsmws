__author__ = 'root'



def main():
    last_arfcns = [12, 22, 32, 33]
    strengths = dict(zip(last_arfcns, [-0.001 for _ in range(0, len(last_arfcns))]))

    print(strengths)