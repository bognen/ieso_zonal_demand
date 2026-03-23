FROM public.ecr.aws/lambda/python:3.11

# Copy requirements file
COPY requirements.txt ${LAMBDA_TASK_ROOT}

# Install the dependencies
RUN pip install -r requirements.txt

# Copy the function code
COPY ieso_zonal_demand_to_s3.py ${LAMBDA_TASK_ROOT}

# Set the CMD to your handler
CMD [ "ieso_zonal_demand_to_s3.lambda_handler" ]
