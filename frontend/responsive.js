/* Labels keep table fields understandable when rows stack on narrow screens. */
(() => {
  'use strict';
  function apply(root=document) {
    const tables=[...root.querySelectorAll('table.tbl:not(#recordTable)')];
    if(root.matches && root.matches('table.tbl:not(#recordTable)'))tables.unshift(root);
    for(const table of tables){
      const headers=[...table.querySelectorAll('thead th')].map(th=>th.textContent.trim());
      if(!headers.length)continue;
      table.classList.add('responsive-table');
      for(const row of table.querySelectorAll('tbody tr')){
        [...row.children].forEach((cell,index)=>{
          if(cell.colSpan>1)cell.classList.add('table-message');
          else if(cell.dataset.label!==headers[index])cell.dataset.label=headers[index]||'';
        });
      }
    }
  }
  window.TLResponsive={apply};
})();
