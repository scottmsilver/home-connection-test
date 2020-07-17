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
    # force "Mega" as in Megabits 
    ax.ticklabel_format(axis="y", scilimits=(6,6))
    ax.autoscale(enable=True)

    pd.set_option('display.float_format', lambda x: '%.2e' % x)
    
    def addTextRow(base, addition):
        return base + "\n" + addition

    figureText = "%s" % df[column].describe()
    x = df.tail(10)
    i = 0
    print(figureText)
    for index,row in x.iterrows():
        print(row[column])
        
        figureText = addTextRow(figureText, "last%d    %5.2f" % (i, row[column]))
        i = i + 1
        
    plt.figtext(1.0, 0.2, figureText, fontname='monospace', bbox={'facecolor':'grey', 'alpha':0.5, 'pad':10})
    
                               
    # Save the graph.
    plt.savefig(outputFile, bbox_inches='tight')
    # plt.show()

graph(column, outputFile)
