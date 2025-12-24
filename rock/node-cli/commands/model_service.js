const fs = require('fs-extra');
const path = require('path');

const MODEL_SERVICE_DIR = 'data/cli/model';
const PID_FILE = path.join(MODEL_SERVICE_DIR, 'pid.txt');

const modelServiceCommand = {
  command: 'model-service command',
  describe: 'Model service operations',
  builder: (yargs) => {
    return yargs
      .command(
        'start',
        'Start model service',
        (yargs) => {
          return yargs.option('type', {
            describe: 'Type of model service',
            type: 'string',
            choices: ['local', 'proxy'],
            default: 'local'
          });
        },
        async (argv) => {
          await startModelService(argv);
        }
      )
      .command(
        'watch-agent',
        'Watch agent status, if stopped, send SESSION_END',
        (yargs) => {
          return yargs.option('pid', {
            describe: 'PID of agent process to watch',
            type: 'number',
            demandOption: true
          });
        },
        async (argv) => {
          await watchAgent(argv);
        }
      )
      .command(
        'stop',
        'Stop model service',
        () => { },
        async () => {
          await stopModelService();
        }
      )
      .command(
        'anti-call-llm',
        'Anti call llm, input is response of llm, output is the next request to llm',
        (yargs) => {
          return yargs
            .option('index', {
              describe: 'Index of last llm call, start from 0',
              type: 'number',
              demandOption: true
            })
            .option('response', {
              describe: 'Response of last llm call',
              type: 'string'
            });
        },
        async (argv) => {
          await antiCallLlm(argv);
        }
      );
  },
  handler: async () => {
    // This will be handled by subcommands
  }
};

const startModelService = async (argv) => {
  // Create model service directory if it doesn't exist
  await fs.ensureDir(MODEL_SERVICE_DIR);

  // Check if model service already exists
  if (await modelServiceExists()) {
    console.error('Model service already exists, please run \'rock model-service stop\' first');
    process.exit(1);
    return;
  }

  console.log('Starting model service...');
  // Implementation would go here
  // This would call the actual model service start functionality
  const serviceType = argv.type;
  console.log(`Starting model service with type: ${serviceType}`);

  // In a real implementation, this would start the actual service and return a PID
  const mockPid = Math.floor(Math.random() * 10000) + 1000; // Mock PID for demonstration
  console.log(`Model service started, PID: ${mockPid}`);

  // Write PID to file
  await fs.writeFile(PID_FILE, mockPid.toString());
};

const watchAgent = async (argv) => {
  const agentPid = argv.pid;
  console.log(`Starting to watch agent process, PID: ${agentPid}`);
  // Implementation would go here
  // This would call the actual watch agent functionality
};

const stopModelService = async () => {
  if (!(await modelServiceExists())) {
    console.log('Model service does not exist, skipping');
    return;
  }

  console.log('Stopping model service...');
  // Read PID from file
  const pid = await fs.readFile(PID_FILE, 'utf8');
  console.log(`Model service exists, PID: ${pid}`);

  // Implementation would go here
  // This would call the actual model service stop functionality
  try {
    process.kill(parseInt(pid), 'SIGTERM');
    console.log(`Model service with PID ${pid} terminated`);
  } catch (e) {
    console.error(`Could not stop process with PID ${pid}: ${e.message}`);
  }

  // Remove PID file
  await fs.remove(PID_FILE);
  console.log('Model service stopped');
};

const antiCallLlm = async (argv) => {
  console.log('Starting anti call llm...');
  // Implementation would go here
  // This would call the actual anti call llm functionality
  const index = argv.index;
  const response = argv.response;

  console.log(`Index: ${index}`);
  if (response) {
    console.log(`Response: ${response}`);
  }

  // In a real implementation, this would return the next request
  const nextRequest = `Next request for call #${index}`;
  console.log(nextRequest); // This is what gets printed to stdout
};

const modelServiceExists = async () => {
  const exists = await fs.pathExists(PID_FILE);
  if (exists) {
    const pid = await fs.readFile(PID_FILE, 'utf8');
    console.log(`Model service exists, PID: ${pid}`);
  }
  return exists;
};

module.exports = modelServiceCommand;