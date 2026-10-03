import readline from 'node:readline/promises';

export async function pauseForEnter(message = 'Press Enter to continue...') {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  try {
    await rl.question(`${message}\n`);
  } finally {
    rl.close();
  }
}
