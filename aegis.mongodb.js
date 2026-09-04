// AEGIS MongoDB Playground
// Run with the MongoDB extension or mongosh while the MongoDB service is running.

use('aegis');

// Connection check
db.runCommand({ ping: 1 });

// Collection overview
db.getCollectionNames();

// Recent sensor readings
db.sensor_readings
  .find({}, { _id: 0 })
  .sort({ timestamp: -1 })
  .limit(20);

// Highest-risk debris snapshots
db.debris_snapshots
  .find({}, { _id: 0, timestamp: 1, debris_count: 1, mean_risk: 1, top_10: 1 })
  .sort({ mean_risk: -1 })
  .limit(20);

// Recent ISS positions
db.iss_positions
  .find({}, { _id: 0 })
  .sort({ timestamp: -1 })
  .limit(20);
