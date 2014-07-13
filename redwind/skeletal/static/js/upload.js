var redwind = redwind || {};
redwind.uploads = {
    completeHandler: function(response) {
        console.log("received upload response:", response);
        $("#uploads").append(
            "<a href=\"" + response.path  + "\">" + response.path +  "</a><br />"
        );
    },

    doUpload: function(file) {
        name = file.name;
        size = file.size;
        type = file.type;
        console.log("uploading:", name, size, type);

        var formData = new FormData();
        formData.append('file', file);

        $.ajax({
            url: '/api/upload_file',
            type: 'POST',
            success: completeHandler,
            data: formData,
            cache: false,
            contentType: false,
            processData: false
        });
    },
};

$(document).ready(function(){
    $(":file").change(function() {
        $(this.files).each(function(idx, file) {
            redwind.uploads.doUpload(file);
        });
    });
});
