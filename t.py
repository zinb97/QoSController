import networkx as nx

G = nx.Graph()
G.add_edge("A", "B", weight=4, g=1)
G.add_edge("B", "D", weight=2, g=5)
G.add_edge("A", "C", weight=3, g=1)
G.add_edge("C", "D", weight=4, g=1)

print(nx.shortest_path(G, "A", "D", weight="weight"))
print(nx.shortest_path(G, "A", "D", weight="g"))