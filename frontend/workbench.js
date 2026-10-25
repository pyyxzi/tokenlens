(() => {
  'use strict';
  const nav = document.querySelector('#nav');
  nav.querySelectorAll('a[data-view]').forEach(link => {
    link.setAttribute('role', 'button');
    link.tabIndex = 0;
  });
  nav.addEventListener('keydown', event => {
    if (event.target.matches('a[data-view]') && ['Enter', ' '].includes(event.key)) {
      event.preventDefault();
      event.stopPropagation();
      event.target.click();
    }
  });
})();
