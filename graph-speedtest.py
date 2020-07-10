import matplotlib.pyplot as plt
import dateutil.parser
from dateutil import tz
import matplotlib.dates as mdates
import sys

import pandas as pd

csvFile = sys.argv[1]
column = sys.argv[2]
outputFile = sys.argv[3]

df = pd.read_csv(csvFile, names = ["Server ID", "Sponsor", "Server Name", "Timestamp", "Distance", "Ping", "Download", "Upload", "Share", "IP Address"])
df["Timestamp"] = pd.to_datetime(df['Timestamp'])

def graph(column, outputFile):
    ax = df.plot.scatter("Timestamp", column, marker = '1', s = 2)
    fig = ax.get_figure()
    ax.set_title(column + ' Speed');
    ax.set_ylabel(column +' Rate (Mb/s)')
    locator = mdates.AutoDateLocator(minticks = 5, maxticks = 15, tz = tz.gettz())
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz = tz.gettz()))

    # Save the graph.
    plt.savefig(outputFile, bbox_inches='tight')
    # plt.show()

graph(column, outputFile)
