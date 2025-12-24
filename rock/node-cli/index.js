#!/usr/bin/env node

const yargs = require('yargs');
const { hideBin } = require('yargs/helpers');
const fs = require('fs-extra');
const path = require('path');

// Import command modules
const adminCommand = require('./commands/admin');
const imageCommand = require('./commands/image');
const modelServiceCommand = require('./commands/model_service');

// Configuration management
const loadConfig = (configPath) => {
  if (!configPath) {
    // Try default config file
    const defaultConfigPath = path.join(process.cwd(), '.rock', 'config.ini');
    if (fs.existsSync(defaultConfigPath)) {
      configPath = defaultConfigPath;
    }
  }

  if (configPath && fs.existsSync(configPath)) {
    // Simple INI file parser
    const content = fs.readFileSync(configPath, 'utf8');
    const lines = content.split('\n');
    const config = { extra_headers: {} };

    let currentSection = '';
    lines.forEach(line => {
      line = line.trim();
      if (line.startsWith('[') && line.endsWith(']')) {
        currentSection = line.substring(1, line.length - 1);
      } else if (line.includes('=')) {
        const [key, value] = line.split('=');
        const cleanKey = key.trim();
        const cleanValue = value.trim();

        if (currentSection === 'DEFAULT' || !currentSection) {
          if (cleanKey === 'base_url') {
            config.base_url = cleanValue;
          } else if (cleanKey === 'xrl_authorization') {
            config.extra_headers['xrl-authorization'] = cleanValue;
          } else if (cleanKey === 'cluster') {
            config.extra_headers['cluster'] = cleanValue;
          }
        }
      }
    });

    return config;
  }

  return { base_url: null, extra_headers: {} };
};

// Main CLI setup
const argv = yargs(hideBin(process.argv))
  .usage('ROCK CLI Tool\nUsage: $0 <command> [options]')
  .option('verbose', {
    alias: 'v',
    type: 'boolean',
    description: 'Enable verbose logging'
  })
  .option('config', {
    type: 'string',
    description: 'Path to config file (default: ./.rock/config.ini)'
  })
  .option('base-url', {
    type: 'string',
    description: 'ROCK server base URL (overrides config file)'
  })
  .option('auth-token', {
    type: 'string',
    description: 'ROCK authorization token (overrides config file)'
  })
  .option('cluster', {
    type: 'string',
    description: 'ROCK cluster (overrides config file)'
  })
  .option('extra-header', {
    alias: 'H',
    type: 'array',
    description: 'Extra HTTP headers in format "Key=Value". Can be used multiple times.',
    coerce: (headers) => {
      const result = {};
      if (headers) {
        headers.forEach(header => {
          if (header.includes('=')) {
            const [key, value] = header.split('=', 2);
            result[key.trim()] = value.trim();
          } else {
            console.warn(`Invalid header format: ${header}. Expected format: 'Key=Value'`);
          }
        });
      }
      return result;
    }
  })
  .middleware((argv) => {
    // Load configuration file and merge with command line arguments
    const config = loadConfig(argv.config);

    // Command line arguments take precedence over config file
    argv.baseUrl = argv.baseUrl || config.base_url;
    argv.authToken = argv.authToken || config.extra_headers['xrl-authorization'];
    argv.cluster = argv.cluster || config.extra_headers['cluster'];

    // Merge extra headers: config file + command line
    argv.extraHeaders = { ...config.extra_headers, ...argv.extraHeader };
  })
  .command(adminCommand)
  .command(imageCommand)
  .command(modelServiceCommand)
  .demandCommand(1, 'Please provide a command')
  .help()
  .alias('help', 'h')
  .epilog(`
Examples:
  # Start admin service
  $0 admin start

  # Stop admin service
  $0 admin stop

  # Build an image
  $0 image build --dockerfile ./Dockerfile --tag my-image:latest

  # Push an image to registry
  $0 image push --image my-image:latest --registry registry.example.com

  # Pull an image from registry
  $0 image pull --image my-image:latest --registry registry.example.com

  # Watch agent status, if stopped, send SESSION_END
  $0 model-service watch-agent

  # start model services
  $0 model-service start

  # stop model service
  $0 model-service stop
  `)
  .argv;

module.exports = argv;