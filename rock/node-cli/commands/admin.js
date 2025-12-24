const fs = require('fs-extra');
const path = require('path');
const { spawn } = require('child_process');
const ps = require('ps-node');

const adminCommand = {
  command: 'admin action',
  describe: 'Admin operations',
  builder: (yargs) => {
    return yargs
      .positional('action', {
        describe: 'Admin action pstart, stop)',
        type: 'string',
        choices: ['start', 'stop']
      })
      .option('env', {
        type: 'string',
        default: 'local',
        describe: 'Admin service environment'
      });
  },
  handler: async (argv) => {
    if (argv.action === 'start') {
      await startAdminService(argv);
    } else if (argv.action === 'stop') {
      await stopAdminService();
    } else {
      console.error(`Unknown admin action: ${argv.action}`);
      process.exit(1);
    }
  }
};

const startAdminService = async (argv) => {
  console.log('Starting admin service...');
  // Spawn the admin process
  const child = spawn('admin', ['--env', argv.env], {
    detached: true,
    stdio: 'ignore'
  });

  child.unref();
  console.log('Admin service started');
};

const stopAdminService = async () => {
  console.log('Stopping admin service...');

  // Find admin processes
  const adminProcesses = [];

  ps.lookup({
    command: 'admin'
  }, (err, resultList) => {
    if (err) {
      console.error('Error looking up processes:', err);
      return;
    }

    resultList.forEach((process) => {
      if (process) {
        adminProcesses.push(process.pid);
      }
    });

    if (adminProcesses.length === 0) {
      console.log('No admin processes found running');
      return;
    }

    let stoppedCount = 0;
    adminProcesses.forEach((pid) => {
      try {
        // Try graceful termination first
        process.kill(pid, 'SIGTERM');
        console.log(`Sent SIGTERM to admin process with PID: ${pid}`);

        // Wait a bit for graceful termination
        setTimeout(() => {
          try {
            // Check if process still exists
            process.kill(pid, 0); // Check if process exists
          } catch (e) {
            // Process has terminated
            stoppedCount++;
            console.log(`Admin process ${pid} terminated gracefully`);
            return;
          }

          // Force kill if still running
          try {
            process.kill(pid, 'SIGKILL');
            stoppedCount++;
            console.log(`Admin process ${pid} killed forcefully`);
          } catch (e) {
            console.warn(`Could not kill process ${pid}: ${e.message}`);
          }
        }, 5000); // Wait 5 seconds before force killing
      } catch (e) {
        console.warn(`Could not access process with PID: ${pid}: ${e.message}`);
      }
    });

    if (stoppedCount > 0) {
      console.log(`Successfully stopped ${stoppedCount} admin process(es)`);
    }
  });
};

module.exports = adminCommand;