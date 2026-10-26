const app = document.querySelector<HTMLElement>('#app');

if (app) {
  const heading = document.createElement('h1');
  heading.textContent = 'Document OCR';
  app.append(heading);
}
