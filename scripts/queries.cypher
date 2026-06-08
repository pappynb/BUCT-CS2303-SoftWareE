// Count artifacts by museum.
MATCH (a:Artifact)-[:COLLECTED_BY]->(m:Museum)
RETURN m.name AS museum, count(a) AS artifact_count
ORDER BY artifact_count DESC;

// Find artifacts from a dynasty.
MATCH (a:Artifact)-[:BELONGS_TO_DYNASTY]->(d:Dynasty {name: "Ming"})
RETURN a.name, a.period_text, a.detail_url
ORDER BY a.name;

// Find ceramic artifacts and their museums.
MATCH (a:Artifact)-[:HAS_TYPE]->(:Category {name: "Ceramics"})
MATCH (a)-[:COLLECTED_BY]->(m:Museum)
RETURN a.name AS artifact, m.name AS museum, a.detail_url AS url;

// Show artifact-centered graph.
MATCH path = (a:Artifact)-[r]-(n)
RETURN path
LIMIT 50;

// Find artists and their works.
MATCH (a:Artifact)-[:CREATED_BY]->(artist:Artist)
RETURN artist.name AS artist, collect(a.name) AS works, count(a) AS work_count
ORDER BY work_count DESC;
