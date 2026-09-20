"""Function words ignored in plain-word queries.

Every term of a plain query must occur in a note, so a filler word acts as a hard
filter: "wie richte ich shorewall mit fail2ban ein" would only find notes that also
contain "wie", "ich", "mit" and "ein". Dropping them leaves the words the question is
about. The index itself keeps every word, so a quoted phrase or any other FTS5 query
still matches them exactly.

Deliberately short and limited to words that carry no topic. Left out on purpose:
"it" (IT as in IT-Sicherheit), "weg", "machen", "viel", "weiter".
"""

GERMAN = frozenset("""
aber alle allem allen aller alles als also am an auch auf aus bei bin bis bist da damit
dann das dass daß dein deine dem den denn der des dessen dich die dies diese diesem diesen
dieser dieses dir doch dort du durch ein eine einem einen einer eines er es euer eure für
hab habe haben hat hatte hatten hier hin hinter ich ihm ihn ihnen ihr ihre ihrem ihren
ihrer im in ins ist ja jede jedem jeden jeder jedes jener kann kein keine keinem keinen
keiner können könnte man mein meine mich mir mit muss musste nach nicht nichts noch nun
nur ob oder ohne sehr sein seine sich sie sind so soll sollte sondern über um und uns
unser unter vom von vor während wann war waren warum was weil welche welchem welchen
welcher welches wenn wer werde werden wie wieder wieso will wir wird wo wollen wurde
wurden zu zum zur zwar zwischen
""".split())

ENGLISH = frozenset("""
a an and are as at be been but by can did do does for from had has have how i if in into
is its my not of on or our should than that the their then there these they this those
to was we were what when where which who why with would you your
""".split())

STOPWORDS = GERMAN | ENGLISH
