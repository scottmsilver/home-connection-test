import json
import sys

responses = []

iperfXmlFile = sys.argv[1]
outputGraphFile = sys.argv[2]

with open(iperfXmlFile) as json_file:
    foo = []
    for cnt, line in enumerate(json_file):
        foo.append(line)
        if line[0] == '}':
            y = ''.join(foo)
            responses.append(json.loads(y))
            foo = []

import matplotlib.pyplot as plt
import csv
import dateutil.parser
import pytz
import matplotlib.dates as mdates

x=[]
y=[]
skippedRecords = 0
for response in responses:
    # These dates are in Zulu (GMT) Time and in ISO 8601 format.
    try:
        jitterMs = response["end"]["sum"]["jitter_ms"]
        packetLossPercent = response["end"]["sum"]["lost_percent"]
        timeInUtc = response["start"]["timestamp"]["time"]
        bitsPerSecond = response["end"]["sum"]["bits_per_second"]

        measurementDateZuluTime = dateutil.parser.parse(timeInUtc)
        x.append(measurementDateZuluTime)
        uploadBandwidthMegaBitsPerSecond = packetLossPercent;
        y.append(uploadBandwidthMegaBitsPerSecond)
    except:
        skippedRecords = skippedRecords + 1

print("skipped records: %d" % skippedRecords)

f, (ax, ax2) = plt.subplots(2, 1, sharex=True)

# plot the same data on both axes
ax.scatter(x, y, s=0.2)
ax2.scatter(x, y, s=0.2)

# zoom-in / limit the view to different portions of the data
ax.set_ylim(2, 30)  # outliers only
ax2.set_ylim(0, 2)  # most of the data

# hide the spines between ax and ax2
ax.spines['bottom'].set_visible(False)
ax2.spines['top'].set_visible(False)
ax.xaxis.tick_top()
ax.tick_params(labeltop=False)  # don't put tick labels at the top
ax2.xaxis.tick_bottom()

# This looks pretty good, and was fairly painless, but you can get that
# cut-out diagonal lines look with just a bit more work. The important
# thing to know here is that in axes coordinates, which are always
# between 0-1, spine endpoints are at these locations (0,0), (0,1),
# (1,0), and (1,1).  Thus, we just need to put the diagonals in the
# appropriate corners of each of our axes, and so long as we use the
# right transform and disable clipping.

d = .015  # how big to make the diagonal lines in axes coordinates
# arguments to pass to plot, just so we don't keep repeating them
kwargs = dict(transform=ax.transAxes, color='k', clip_on=False)
ax.plot((-d, +d), (-d, +d), **kwargs)        # top-left diagonal
ax.plot((1 - d, 1 + d), (-d, +d), **kwargs)  # top-right diagonal

kwargs.update(transform=ax2.transAxes)  # switch to the bottom axes
ax2.plot((-d, +d), (1 - d, 1 + d), **kwargs)  # bottom-left diagonal
ax2.plot((1 - d, 1 + d), (1 - d, 1 + d), **kwargs)  # bottom-right diagonal

# What's cool about this is that now if we vary the distance between
# ax and ax2 via f.subplots_adjust(hspace=...) or plt.subplot_tool(),
# the diagonal lines will move accordingly, and stay right at the tips
# of the spines they are 'breaking'
ax.set_title('Packet Loss');
ax.set_ylabel('Packet Loss (%)')
locator = mdates.AutoDateLocator(minticks=5, maxticks=15, tz = pytz.timezone('America/Los_Angeles'))
ax.xaxis.set_major_locator(locator)
ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz = pytz.timezone('America/Los_Angeles')))

# Show some stats about the data on the graph.
import pandas as pd

df = pd.DataFrame(y, index =x, columns =['Packet Loss'])

def addTextRow(base, addition):
    return base + "\n" + addition

figureText = "%s" % df.describe()
if len(y) > 4:
    for i in range(1, 4):
        figureText= addTextRow(figureText, "last%d    %5.2f" % (i-1, y[-i]))

plt.figtext(1.0, 0.2, figureText, fontname='monospace', bbox={'facecolor':'grey', 'alpha':0.5, 'pad':10})

plt.savefig(outputGraphFile, bbox_inches='tight')

#plt.show()
