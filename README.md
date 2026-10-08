# Pickleball Courts NYC
 
Pickle is an agent where users can provide their address and recieve the 3 closest pickball courts to their location in NYC. Additioannly, it provdoes users on time of sunset and sunrise, if courts are public or private(if available), and the number of courts. 

## Tools
 
- `find_pickleball_courts`: Finds the 3 nearest court sites to the provided location ranked by travel distance (not euclidean). It provides information about number of courts and private or public when applciable. 
- `get_sun_times`: Returns the current time in NYC and the sunrise and sunset for that day. If it is past sunset, it provides tomorrow's sunrise and sunset time to encourage play tomorrow. 
- `get_distance_to_court`: Returns the distance (miles) and travel time between users provided location and a specific court.
- `can_i_play_before_dark`: Calculates travel time to a court and how many minutes of daylight remain when you arrive.
The data is from OpenStreetMap(Nominatim, Overpass), Open-Meteo, and the OSM routing server.
 
## How to use it
 
The user types a New York address or park into the chat box and it will return the 3 closest . Mention how you're getting there (walking, biking or driving) if you want courts ranked for that mode; otherwise it assumes walking. Pickle is limited to New York City.

Example queries:
- "Columbia University"
- "117 MacDougal St, New York, NY 10012 and can I go now?"
- "Gramercy Park"

## Link
https://pickleball-agent-git-1059900120516.us-east1.run.app/
