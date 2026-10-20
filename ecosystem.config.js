const path = require('path');
module.exports = {
  apps: [{
    name: 'telegram-rights',
    cwd: __dirname,
    script: path.join(__dirname, 'bot.py'),
    interpreter: path.join(__dirname, '.venv', 'bin', 'python'),
    exec_mode: 'fork',
    instances: 1,
    autorestart: true,
    restart_delay: 5000,
    kill_timeout: 20000,
    max_memory_restart: '250M',
    time: true,
    env: { PYTHONUNBUFFERED: '1' }
  }]
};
