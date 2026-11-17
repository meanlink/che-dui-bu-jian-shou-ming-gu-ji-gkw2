import {api} from './client.js';
export const trials={
 list:()=>api('/trials'),
 get:id=>api('/trials/'+id),
 open:data=>api('/trials',{method:'POST',body:JSON.stringify(data)}),
 enroll:(id,data)=>api('/trials/'+id+'/enrollments',{method:'POST',body:JSON.stringify(data)}),
 observe:(id,data)=>api('/trials/'+id+'/observations',{method:'POST',body:JSON.stringify(data)}),
 close:id=>api('/trials/'+id+'/close',{method:'POST'}),
 groups:id=>api('/trials/'+id+'/groups'),
 estimates:(id,query)=>api('/trials/'+id+'/estimates'+query)
};
